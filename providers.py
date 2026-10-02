# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Providers -- configured LLM backends (2026-09-30). The only module
with raw SQL against `providers`. See models.py for the alias+provider+
model-name roster that points AT these, and chat.py's call_for_model()/
call_via_chain() for how a turn actually dispatches through one.

Four provider types, TYPES below:
  openrouter       -- API key, https://openrouter.ai. The only "bring your
                      own key" type; everything else is OAuth.
  anthropic_oauth  -- a Claude Pro/Max subscription, via Claude Code's own
                      public OAuth client. Anthropic's redirect lands on
                      their own page showing a code to copy -- a manual-
                      paste flow, no listener needed on our end.
  openai_oauth     -- a ChatGPT Plus/Pro (Codex) subscription, via Codex
                      CLI's own public OAuth client. OpenAI's registered
                      redirect is a LOOPBACK URL (localhost:1455) meant
                      for a CLI running on the same machine as the
                      browser -- Nori is a server, so instead of binding
                      that port, the admin completes login and pastes
                      back the URL their browser failed to load (the code
                      is still in it) the same way the Anthropic flow's
                      code gets pasted back.
  github_copilot   -- device-code flow (RFC 8628), GitHub's own public CLI
                      client id. No browser redirect at all -- show a user
                      code, admin enters it at github.com/login/device,
                      we poll. A GitHub access token alone isn't a Copilot
                      completions token; access_token_for() mints/refreshes
                      the short-lived one (~30 min) from it on demand.
  xai_api_key      -- API key, https://api.x.ai -- xAI's own official,
                      documented API (OpenAI-SDK-compatible), not a
                      reverse-engineered path at all, same footing as
                      openrouter/openai_api_key/anthropic_api_key.
  xai_oauth        -- a SuperGrok/X Premium+ subscription, via Grok
                      Build's (xAI's own official CLI coding agent) public
                      OAuth client -- also device-code flow, verified
                      against a real, actively-maintained third-party
                      client's source (stnly/pi-grok, itself built on the
                      Hermes agent's xai-oauth flow), not guessed.

None of this beyond xai_api_key is vendor-supported. These are the same unofficial client
IDs/endpoints Claude Code, Codex CLI, and VS Code's own Copilot Chat
extension use internally -- verified against public references (issue
trackers, the actively-maintained ericc-ch/copilot-api gateway) while this
was built (2026-09-30), not invented. They can change without notice; see
chat.py's own adapters for the honesty notes on the two OAuth completion
calls specifically, which needed more guesswork than the OAuth mechanics
here did.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

import crypto
import store

TYPES: dict[str, dict] = {
    "openrouter":      {"label": "OpenRouter",                    "auth": "api_key"},
    "anthropic_oauth":  {"label": "Anthropic (Claude Pro/Max)",    "auth": "oauth_manual"},
    "anthropic_api_key": {"label": "Anthropic (API key)",         "auth": "api_key"},
    "openai_oauth":     {"label": "OpenAI (ChatGPT / Codex)",     "auth": "oauth_manual"},
    "openai_api_key":   {"label": "OpenAI (API key)",             "auth": "api_key"},
    "github_copilot":   {"label": "GitHub Copilot",               "auth": "oauth_device"},
    "xai_api_key":      {"label": "xAI (API key)",                 "auth": "api_key"},
    "xai_oauth":        {"label": "xAI (Grok / SuperGrok subscription)", "auth": "oauth_device"},
}
# Provider types whose "add" form is just a label + API key (2026-09-30,
# see create_api_key()) -- same essential model as OpenRouter's, just a
# different base URL per vendor, both reusing chat.py's plain
# call()/OpenAI-chat-completions-shaped path directly (OpenAI) or a
# dedicated Messages-API adapter (Anthropic) rather than any OAuth
# mechanics at all. No Claude-Code-identity mimicry, no oauth-only beta
# headers -- those exist ONLY to get an OAuth token past Anthropic's
# Cloudflare gate; a real API key is a normal, fully-supported call.
API_KEY_TYPES = ("openrouter", "anthropic_api_key", "openai_api_key", "xai_api_key")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# No OPENAI_API_KEY_URL constant here -- chat.py's _call_openai_api_key
# reuses _call_direct(), whose _DIRECT_PROVIDERS["openai"]["url"] is
# already the same api.openai.com endpoint; one source of truth, not two.

# -- Anthropic (Claude Code's own public OAuth client) ---------------------
_ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_ANTHROPIC_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
# api.anthropic.com, NOT console.anthropic.com (2026-09-30, real-world
# fix found after the console host's own token endpoint 403'd every
# exchange with Cloudflare error 1010/browser_signature_banned --
# confirmed by multiple independent OAuth-client implementations: the
# console host sits behind a Cloudflare managed challenge that only
# admits requests that already look like Claude Code's own client;
# api.anthropic.com serves the identical /v1/oauth/token endpoint
# without that challenge). _ANTHROPIC_HEADERS below still sent on every
# request to either host, belt-and-braces -- the mimicry headers alone
# were reported to fix this for some, the host swap for others.
_ANTHROPIC_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
_ANTHROPIC_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
_ANTHROPIC_SCOPE = "org:create_api_key user:profile user:inference"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
# Claude Code's own request fingerprint -- Cloudflare's rule in front of
# Anthropic's OAuth endpoints admits requests that carry this, and 403s
# (error 1010) ones that don't (confirmed 2026-09-30 against a real
# console.anthropic.com 403 -- see _ANTHROPIC_TOKEN_URL's own comment).
# Sent on the token exchange/refresh below; chat.py's own Messages-API
# adapter carries the matching User-Agent/anthropic-beta pair for the
# same reason on the inference call itself.
_ANTHROPIC_HEADERS = {"Content-Type": "application/json",
                     "User-Agent": "claude-cli/1.0.56 (external, cli)",
                     "anthropic-version": "2023-06-01",
                     "X-Stainless-Retry-Count": "0",
                     "X-Stainless-Lang": "js",
                     "X-Stainless-Package-Version": "0.55.1",
                     "X-Stainless-OS": "Linux",
                     "X-Stainless-Arch": "x64",
                     "X-Stainless-Runtime": "node",
                     "X-Stainless-Runtime-Version": "v20.18.1"}

# -- OpenAI (Codex CLI's own public OAuth client) ---------------------------
_OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_OPENAI_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
_OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
_OPENAI_REDIRECT_URI = "http://localhost:1455/auth/callback"
_OPENAI_SCOPE = "openid profile email offline_access"
OPENAI_CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"

# -- GitHub Copilot (device flow) -------------------------------------------
_GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"
_GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
_GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_SCOPE = "read:user"
_COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
# Spoofed to match VS Code's own Copilot Chat extension -- Copilot's token
# endpoint is undocumented and reverse-engineered (see module docstring);
# these are the header values every current open-source Copilot gateway
# sends, not a guess.
_COPILOT_HEADERS = {
    "editor-version": "vscode/1.111.0",
    "editor-plugin-version": "copilot-chat/0.26.7",
    "user-agent": "GitHubCopilotChat/0.26.7",
}
COPILOT_COMPLETIONS_URL = "https://api.githubcopilot.com/chat/completions"

# -- xAI (Grok Build's own public OAuth client -- xAI's official CLI coding
# agent for SuperGrok/X Premium+ subscribers) --------------------------------
# Verified directly against stnly/pi-grok's real source (itself built on
# the Hermes agent's xai-oauth flow), not a summary/guess -- see module
# docstring. Device-code flow (RFC 8628), same shape as GitHub's above,
# different vendor endpoints/client id/headers.
_XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
_XAI_DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
_XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"
_XAI_SCOPE = ("openid profile email offline_access grok-cli:access api:access "
             "conversations:read conversations:write")
# The subscription proxy gates on this version label and rejects anything it
# doesn't admit -- "Grok CLI is outdated: installed 0.2.101, required 1.0.13
# or later" (2026-10-02, the operator's own report; the reference client,
# pi-grok, bumped to 1.0.46 for the same reason the day before). It's a label
# we have to keep current, not a version of anything Nori runs, so it's
# overridable without a code change or rebuild: set NORI_XAI_CLIENT_VERSION in
# .env and restart. The floor will keep moving.
_XAI_CLIENT_VERSION = (os.environ.get("NORI_XAI_CLIENT_VERSION") or "").strip() or "1.0.46"
_XAI_DEVICE_HEADERS = {"Content-Type": "application/x-www-form-urlencoded",
                       "x-grok-client-version": _XAI_CLIENT_VERSION, "x-grok-client-surface": "cli"}


def _xai_platform_label() -> str:
    """The `(os; arch)` suffix the reference client puts in its User-Agent
    ("macos; aarch64", "windows; x86_64") -- mapped the same way, so ours
    reads like a native client's rather than a bare name/version."""
    import platform
    system = {"darwin": "macos", "windows": "windows"}.get(platform.system().lower(), platform.system().lower())
    arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(platform.machine().lower(), platform.machine().lower())
    return f"{system}; {arch}"
# The subscription (not pay-per-token) inference path -- a plain
# api.x.ai API key does NOT ride this; it hits api.x.ai directly instead
# (see xai_api_key/XAI_API_KEY_URL below). Responses-API-shaped
# ("api: openai-responses" in pi-grok's own provider registration).
XAI_PROXY_RESPONSES_URL = "https://cli-chat-proxy.grok.com/v1/responses"
XAI_API_KEY_URL = "https://api.x.ai/v1/chat/completions"


def _xai_proxy_headers(model_id: str | None = None) -> dict:
    headers = {"User-Agent": f"grok-shell/{_XAI_CLIENT_VERSION} ({_xai_platform_label()})",
              "x-grok-client-identifier": "grok-shell",
              "x-grok-client-version": _XAI_CLIENT_VERSION, "x-grok-client-mode": "interactive",
              "X-XAI-Token-Auth": "xai-grok-cli", "x-authenticateresponse": "authenticate-response"}
    if model_id:
        headers["x-grok-model-override"] = model_id
    return headers


_HTTP_TIMEOUT_S = 20


def _post_json(url: str, body: dict | None, *, headers: dict, method: str = "POST") -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_form(url: str, fields: dict, *, headers: dict) -> dict:
    """Same shape as _post_json but application/x-www-form-urlencoded --
    xAI's device-code/token endpoints take form-encoded bodies, not JSON
    (verified against pi-grok's real source, unlike GitHub's, which are
    JSON)."""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ── CRUD ────────────────────────────────────────────────────────────────

def list_all(workspace_id: int) -> list[dict]:
    """UI-facing -- every secret column stripped. Use get() internally
    (chat.py's dispatch, api_key_for()/access_token_for()) when the raw
    encrypted columns are actually needed."""
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM providers WHERE workspace_id=? ORDER BY created_ts", (workspace_id,)).fetchall())
    return [_public(dict(r)) for r in rows]


def _public(row: dict) -> dict:
    row = dict(row)
    for k in ("api_key_enc", "access_token_enc", "refresh_token_enc", "pending_enc"):
        row.pop(k, None)
    return row


def get(provider_id: int) -> dict | None:
    r = store.read(lambda c: c.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone())
    return dict(r) if r else None


def create_api_key(workspace_id: int, type_: str, label: str, api_key: str,
                   created_by: int) -> tuple[bool, str]:
    if type_ not in API_KEY_TYPES:
        return False, f"{type_!r} isn't an API-key provider type"
    label = (label or "").strip() or TYPES[type_]["label"]
    api_key = (api_key or "").strip()
    if not api_key:
        return False, "an API key is required"
    store.write(lambda c: c.execute(
        "INSERT INTO providers(workspace_id, type, label, status, api_key_enc, enabled, "
        "created_ts, created_by) VALUES (?,?,?,?,?,1,?,?)",
        (workspace_id, type_, label, "connected", crypto.encrypt(api_key), time.time(), created_by)))
    return True, "added"


def delete(provider_id: int) -> None:
    """Same FK-ordering fix as models.delete() -- models.provider_id is a
    real FK to providers(id), so any model still pointing at this
    provider has to be unlinked (not deleted -- the alias/model_name the
    admin configured survives, just shown as "needs a provider" until
    relinked) before the provider row itself can go."""
    store.write(lambda c: c.execute("UPDATE models SET provider_id=NULL WHERE provider_id=?", (provider_id,)))
    store.write(lambda c: c.execute("DELETE FROM providers WHERE id=?", (provider_id,)))


def set_enabled(provider_id: int, enabled: bool) -> None:
    store.write(lambda c: c.execute(
        "UPDATE providers SET enabled=? WHERE id=?", (1 if enabled else 0, provider_id)))


def api_key_for(provider: dict) -> str:
    if not provider.get("api_key_enc"):
        raise ValueError(f"provider {provider.get('label')!r} has no API key configured")
    return crypto.decrypt(provider["api_key_enc"])


# ── Anthropic / OpenAI: manual-code OAuth ──────────────────────────────

def begin_oauth_manual(workspace_id: int, provider_type: str, label: str,
                       created_by: int) -> tuple[bool, str]:
    """Creates a pending provider row, returns (True, authorize_url) to
    show the admin, or (False, error). They complete login in their own
    browser and paste the resulting code/URL back via
    complete_oauth_manual()."""
    if provider_type not in ("anthropic_oauth", "openai_oauth"):
        return False, f"{provider_type!r} isn't a manual-code OAuth provider"
    label = (label or "").strip() or TYPES[provider_type]["label"]
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    if provider_type == "anthropic_oauth":
        params = {"code": "true", "client_id": _ANTHROPIC_CLIENT_ID, "response_type": "code",
                  "redirect_uri": _ANTHROPIC_REDIRECT_URI, "scope": _ANTHROPIC_SCOPE,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": state}
        url = _ANTHROPIC_AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    else:
        params = {"client_id": _OPENAI_CLIENT_ID, "response_type": "code",
                  "redirect_uri": _OPENAI_REDIRECT_URI, "scope": _OPENAI_SCOPE,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": state}
        url = _OPENAI_AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    # authorize_url persisted here too (2026-09-30), not just returned --
    # pending_display() re-reads it from the DB so the settings page can
    # keep showing a real, copyable link/code across a reload instead of
    # only in the one-shot flash message that used to be the only place
    # it appeared (hard to copy, gone the moment you navigated away).
    pending = crypto.encrypt(json.dumps(
        {"verifier": verifier, "state": state, "ts": time.time(), "authorize_url": url}))
    store.write(lambda c: c.execute(
        "INSERT INTO providers(workspace_id, type, label, status, pending_enc, enabled, "
        "created_ts, created_by) VALUES (?,?,?,?,?,1,?,?)",
        (workspace_id, provider_type, label, "unconfigured", pending, time.time(), created_by)))
    return True, url


def _extract_code(provider_type: str, pasted: str) -> tuple[str | None, str | None]:
    """Anthropic's own page shows `code#state` directly (Claude Code's own
    documented format); OpenAI's admin is pasting back a URL their browser
    failed to load (localhost:1455/...?code=...&state=...) since nothing's
    listening there -- accept either a bare code or a full URL/query for
    both, rather than making the admin know which shape to expect."""
    # Collapse ALL whitespace, not just trim the ends -- a code copied out
    # of a terminal-style display can wrap onto a second line, leaving a
    # newline or stray space buried in the middle that a plain .strip()
    # wouldn't catch and that silently corrupts the code (invalid_grant
    # from Anthropic looks identical whether the code is wrong, expired,
    # or just mangled like this -- found live, 2026-09-30).
    pasted = "".join((pasted or "").split())
    if not pasted:
        return None, None
    if "code=" in pasted:
        parsed = urllib.parse.urlparse(pasted if "://" in pasted else "http://x/?" + pasted)
        qs = urllib.parse.parse_qs(parsed.query)
        code = (qs.get("code") or [None])[0]
        state = (qs.get("state") or [None])[0]
        return code, state
    if provider_type == "anthropic_oauth" and "#" in pasted:
        code, _, state = pasted.partition("#")
        return code or None, state or None
    return pasted, None


def complete_oauth_manual(provider_id: int, pasted: str) -> tuple[bool, str]:
    provider = get(provider_id)
    if provider is None:
        return False, "no such provider"
    if provider["type"] not in ("anthropic_oauth", "openai_oauth") or not provider.get("pending_enc"):
        return False, "this provider isn't waiting on a connect step"
    pending = json.loads(crypto.decrypt(provider["pending_enc"]))
    code, state = _extract_code(provider["type"], pasted)
    if not code:
        return False, "couldn't find a code in what you pasted"
    if state and state != pending["state"]:
        return False, "that doesn't match the connect attempt that's pending -- start over"
    if provider["type"] == "anthropic_oauth":
        token_url, client_id, redirect_uri = _ANTHROPIC_TOKEN_URL, _ANTHROPIC_CLIENT_ID, _ANTHROPIC_REDIRECT_URI
        headers = _ANTHROPIC_HEADERS
    else:
        token_url, client_id, redirect_uri = _OPENAI_TOKEN_URL, _OPENAI_CLIENT_ID, _OPENAI_REDIRECT_URI
        headers = {"Content-Type": "application/json"}
    body = {"grant_type": "authorization_code", "code": code, "state": pending["state"],
            "client_id": client_id, "redirect_uri": redirect_uri, "code_verifier": pending["verifier"]}
    try:
        data = _post_json(token_url, body, headers=headers)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        # invalid_grant is what a genuinely wrong code AND a stale/
        # already-used one (these codes are single-use, ~5 min lived)
        # both look like -- called out explicitly since "start a fresh
        # connect attempt" is the actual fix far more often than a typo.
        if "invalid_grant" in detail:
            return False, (f"token exchange failed ({exc.code}): {detail} -- this code is single-use and "
                           f"expires in a few minutes; remove this provider and start a fresh connect "
                           f"attempt rather than retrying the same code")
        return False, f"token exchange failed ({exc.code}): {detail}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, f"token exchange failed: {exc}"
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    if not access:
        return False, f"no access token in the response: {str(data)[:300]}"
    expires_ts = time.time() + float(data.get("expires_in") or 3600)
    store.write(lambda c: c.execute(
        "UPDATE providers SET status='connected', access_token_enc=?, refresh_token_enc=?, "
        "token_expires_ts=?, pending_enc=NULL WHERE id=?",
        (crypto.encrypt(access), crypto.encrypt(refresh) if refresh else None, expires_ts, provider_id)))
    return True, "connected"


def _refresh_oauth_manual(provider: dict) -> str:
    if provider["type"] == "anthropic_oauth":
        token_url, client_id, headers, poster = _ANTHROPIC_TOKEN_URL, _ANTHROPIC_CLIENT_ID, _ANTHROPIC_HEADERS, _post_json
    elif provider["type"] == "xai_oauth":
        # Same access+refresh+expiry storage shape as the two manual
        # flows above (unlike github_copilot's mint-on-demand pattern) --
        # xAI's device flow hands back a real OAuth token pair, it just
        # uses form-encoding and its own headers/client_id (see
        # _post_form's own docstring).
        token_url, client_id, headers, poster = _XAI_TOKEN_URL, _XAI_CLIENT_ID, _XAI_DEVICE_HEADERS, _post_form
    else:
        token_url, client_id, headers, poster = _OPENAI_TOKEN_URL, _OPENAI_CLIENT_ID, {"Content-Type": "application/json"}, _post_json
    if not provider.get("refresh_token_enc"):
        _mark_needs_reauth(provider["id"])
        raise ValueError(f"provider {provider['label']!r} needs reconnecting (no refresh token on file)")
    refresh_token = crypto.decrypt(provider["refresh_token_enc"])
    body = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    try:
        data = poster(token_url, body, headers=headers)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        _mark_needs_reauth(provider["id"])
        raise ValueError(f"provider {provider['label']!r} needs reconnecting (refresh failed: {exc})") from exc
    access = data.get("access_token")
    refresh = data.get("refresh_token") or refresh_token
    if not access:
        _mark_needs_reauth(provider["id"])
        raise ValueError(f"provider {provider['label']!r} needs reconnecting (refresh returned no token)")
    expires_ts = time.time() + float(data.get("expires_in") or 3600)
    store.write(lambda c: c.execute(
        "UPDATE providers SET status='connected', access_token_enc=?, refresh_token_enc=?, "
        "token_expires_ts=? WHERE id=?",
        (crypto.encrypt(access), crypto.encrypt(refresh), expires_ts, provider["id"])))
    return access


def _mark_needs_reauth(provider_id: int) -> None:
    store.write(lambda c: c.execute(
        "UPDATE providers SET status='needs_reauth' WHERE id=?", (provider_id,)))


# ── GitHub Copilot: device flow ────────────────────────────────────────

_DEVICE_FLOW_TYPES = ("github_copilot", "xai_oauth")


def begin_device_flow(workspace_id: int, provider_type: str, label: str,
                      created_by: int) -> tuple[bool, str, dict | None]:
    if provider_type not in _DEVICE_FLOW_TYPES:
        return False, f"{provider_type!r} isn't a device-flow provider type", None
    if provider_type == "github_copilot":
        url, body, headers = (_GITHUB_DEVICE_CODE_URL, {"client_id": _GITHUB_CLIENT_ID, "scope": _GITHUB_SCOPE},
                              {"Content-Type": "application/json", "Accept": "application/json"})
        poster = _post_json
    else:
        url, body, headers = (_XAI_DEVICE_CODE_URL,
                              {"client_id": _XAI_CLIENT_ID, "scope": _XAI_SCOPE, "referrer": "grok-build"},
                              _XAI_DEVICE_HEADERS)
        poster = _post_form
    try:
        data = poster(url, body, headers=headers)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, f"couldn't start the device flow: {exc}", None
    # user_code/verification_uri persisted here too, same reasoning as
    # begin_oauth_manual's authorize_url -- see pending_display().
    verification_uri = data.get("verification_uri") or data.get("verification_uri_complete")
    pending = crypto.encrypt(json.dumps({
        "device_code": data["device_code"], "interval": data.get("interval", 5), "ts": time.time(),
        "user_code": data["user_code"], "verification_uri": verification_uri}))
    label = (label or "").strip() or TYPES[provider_type]["label"]
    provider_id = store.write(lambda c: c.execute(
        "INSERT INTO providers(workspace_id, type, label, status, pending_enc, enabled, "
        "created_ts, created_by) VALUES (?,?,?,?,?,1,?,?)",
        (workspace_id, provider_type, label, "unconfigured", pending, time.time(), created_by)).lastrowid)
    return True, "waiting for you to authorize", {
        "provider_id": provider_id, "user_code": data["user_code"], "verification_uri": verification_uri}


def check_device_flow(provider_id: int) -> tuple[bool, str]:
    """One poll, called from a "check status" button (see module docstring
    -- Nori has no background-JS polling loop today). Returns (True, ...)
    once the vendor actually has a token; (False, "pending") is normal and
    not an error while the admin hasn't finished authorizing yet."""
    provider = get(provider_id)
    if provider is None or provider["type"] not in _DEVICE_FLOW_TYPES or not provider.get("pending_enc"):
        return False, "this provider isn't waiting on a connect step"
    pending = json.loads(crypto.decrypt(provider["pending_enc"]))
    if provider["type"] == "github_copilot":
        url = _GITHUB_ACCESS_TOKEN_URL
        body = {"client_id": _GITHUB_CLIENT_ID, "device_code": pending["device_code"],
               "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        poster = _post_json
    else:
        url = _XAI_TOKEN_URL
        body = {"client_id": _XAI_CLIENT_ID, "device_code": pending["device_code"],
               "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}
        headers = _XAI_DEVICE_HEADERS
        poster = _post_form
    try:
        data = poster(url, body, headers=headers)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, f"couldn't check yet: {exc}"
    if data.get("access_token"):
        if provider["type"] == "github_copilot":
            # Long-lived GitHub token, stored as-is -- the short-lived
            # Copilot completions token is minted from it on demand (see
            # _mint_copilot_token), never stored directly.
            store.write(lambda c: c.execute(
                "UPDATE providers SET status='connected', refresh_token_enc=?, pending_enc=NULL WHERE id=?",
                (crypto.encrypt(data["access_token"]), provider_id)))
        else:
            # xAI hands back a real access+refresh pair -- same storage
            # shape complete_oauth_manual() uses for Anthropic/OpenAI.
            expires_ts = time.time() + float(data.get("expires_in") or 3600)
            store.write(lambda c: c.execute(
                "UPDATE providers SET status='connected', access_token_enc=?, refresh_token_enc=?, "
                "token_expires_ts=?, pending_enc=NULL WHERE id=?",
                (crypto.encrypt(data["access_token"]),
                 crypto.encrypt(data["refresh_token"]) if data.get("refresh_token") else None,
                 expires_ts, provider_id)))
        return True, "connected"
    err = data.get("error", "authorization_pending")
    if err == "authorization_pending":
        return False, "pending"
    if err == "expired_token":
        return False, "that code expired -- start over"
    return False, f"{provider['type']} said: {err}"


def pending_display(provider: dict) -> dict | None:
    """The subset of a provider's pending connect state that's safe and
    useful to show the admin -- an authorize link (anthropic_oauth/
    openai_oauth) or a verification URL + user code (github_copilot).
    Never the PKCE verifier or device_code. None if there's nothing
    pending (already connected, or never started) -- callers use this to
    render a real, persistent, copyable link/code under a provider's own
    row instead of a one-shot flash message that vanishes on reload."""
    if not provider.get("pending_enc"):
        return None
    pending = json.loads(crypto.decrypt(provider["pending_enc"]))
    if provider["type"] in ("anthropic_oauth", "openai_oauth") and pending.get("authorize_url"):
        return {"authorize_url": pending["authorize_url"]}
    if provider["type"] in _DEVICE_FLOW_TYPES and pending.get("user_code"):
        return {"user_code": pending["user_code"], "verification_uri": pending["verification_uri"]}
    return None


def _mint_copilot_token(provider: dict) -> tuple[str, float]:
    if not provider.get("refresh_token_enc"):
        _mark_needs_reauth(provider["id"])
        raise ValueError(f"provider {provider['label']!r} needs reconnecting (no GitHub token on file)")
    github_token = crypto.decrypt(provider["refresh_token_enc"])
    headers = {"Authorization": f"token {github_token}", "Accept": "application/json", **_COPILOT_HEADERS}
    try:
        data = _post_json(_COPILOT_TOKEN_URL, None, headers=headers, method="GET")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            _mark_needs_reauth(provider["id"])
        raise ValueError(f"provider {provider['label']!r}: couldn't mint a Copilot token ({exc.code})") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ValueError(f"provider {provider['label']!r}: couldn't mint a Copilot token ({exc})") from exc
    token = data.get("token")
    if not token:
        raise ValueError(f"provider {provider['label']!r}: Copilot token response had no token")
    expires_ts = float(data.get("expires_at") or (time.time() + 1500))
    store.write(lambda c: c.execute(
        "UPDATE providers SET access_token_enc=?, token_expires_ts=? WHERE id=?",
        (crypto.encrypt(token), expires_ts, provider["id"])))
    return token, expires_ts


# ── Unified "give me a usable token" for chat.py's adapters ────────────

# Refresh this far ahead of actual expiry -- avoids a call failing mid-
# flight because the token happened to lapse between fetch and use.
_REFRESH_MARGIN_S = 90


def access_token_for(provider: dict) -> str:
    """Decrypted, refreshed-if-needed bearer token for an OAuth provider
    (any of the four OAuth types). Always re-fetches provider from the
    DB first -- the row passed in (e.g. from models._resolve_model_chain's
    join) may be stale if another call refreshed it moments ago."""
    provider = get(provider["id"]) or provider
    if provider["type"] == "github_copilot":
        # The one type with no real refresh_token pair of its own -- a
        # long-lived GitHub token mints a fresh short-lived Copilot
        # completions token on demand instead (see _mint_copilot_token).
        if provider.get("access_token_enc") and (provider.get("token_expires_ts") or 0) > time.time() + _REFRESH_MARGIN_S:
            return crypto.decrypt(provider["access_token_enc"])
        token, _ = _mint_copilot_token(provider)
        return token
    # anthropic_oauth / openai_oauth / xai_oauth -- a real access+refresh
    # token pair, same shape/refresh mechanics for all three (see
    # _refresh_oauth_manual's own per-type branch).
    if provider.get("access_token_enc") and (provider.get("token_expires_ts") or 0) > time.time() + _REFRESH_MARGIN_S:
        return crypto.decrypt(provider["access_token_enc"])
    return _refresh_oauth_manual(provider)


# ── Live model listing (2026-09-30) ─────────────────────────────────────
# "What model names does this provider actually expose" -- so an admin
# picks a real one instead of guessing/typo-ing a slug into the Models
# form. Every type here has a real, documented (or at minimum widely-
# implemented) models-list endpoint EXCEPT openai_oauth, whose ChatGPT-
# backend Codex session has no known equivalent -- that one returns a
# clear "not available" rather than a fabricated list.

def list_models(provider: dict) -> tuple[bool, list[dict] | str]:
    """(True, [{"id": ..., "label": ...}, ...]) on success, sorted by id;
    (False, error message) otherwise -- including "not available for this
    provider type" for openai_oauth, which is a real answer, not a
    failure to catch. `id` is exactly what should go in a Model's
    model_name field."""
    try:
        if provider["type"] == "openrouter":
            return _list_models_openrouter(provider)
        if provider["type"] == "openai_api_key":
            return _list_models_openai(url="https://api.openai.com/v1/models",
                                       headers={"Authorization": f"Bearer {api_key_for(provider)}"})
        if provider["type"] == "anthropic_api_key":
            return _list_models_anthropic(headers={"x-api-key": api_key_for(provider),
                                                    "anthropic-version": "2023-06-01"})
        if provider["type"] == "anthropic_oauth":
            token = access_token_for(provider)
            headers = {"Authorization": f"Bearer {token}", "anthropic-version": "2023-06-01",
                      "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
                      "x-app": "cli", "User-Agent": "claude-cli/1.0.56 (external, cli)"}
            return _list_models_anthropic(headers=headers)
        if provider["type"] == "github_copilot":
            return _list_models_copilot(provider)
        if provider["type"] == "xai_api_key":
            return _list_models_openai(url="https://api.x.ai/v1/models",
                                       headers={"Authorization": f"Bearer {api_key_for(provider)}"})
        if provider["type"] == "xai_oauth":
            # Best-effort: the standard api.x.ai listing, with the OAuth
            # token -- not yet confirmed this is what the subscription
            # proxy itself would return (its own /v1/models, seen in
            # pi-grok's source, wasn't fully characterized before this
            # was built). If the subscription's real catalog differs,
            # that's the next real fix here, same as every other adapter
            # in this file that started as a reasonable first attempt.
            return _list_models_openai(url="https://api.x.ai/v1/models",
                                       headers={"Authorization": f"Bearer {access_token_for(provider)}"})
        if provider["type"] == "openai_oauth":
            return False, ("no known models-list endpoint for the ChatGPT/Codex OAuth session -- "
                          "check Codex's own docs for the exact model name and type it in directly")
        return False, f"unknown provider type {provider['type']!r}"
    except ValueError as exc:
        return False, str(exc)


_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


def _list_models_openrouter(provider: dict) -> tuple[bool, list[dict] | str]:
    headers = {"Accept": "application/json"}
    if provider.get("api_key_enc"):
        headers["Authorization"] = f"Bearer {api_key_for(provider)}"
    try:
        data = _post_json(_OPENROUTER_MODELS_URL, None, headers=headers, method="GET")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, f"couldn't list models: {exc}"
    items = data.get("data") or []
    return True, sorted(({"id": m["id"], "label": m.get("name") or m["id"]}
                         for m in items if m.get("id")), key=lambda m: m["id"])


def _list_models_openai(*, url: str, headers: dict) -> tuple[bool, list[dict] | str]:
    try:
        data = _post_json(url, None, headers={"Accept": "application/json", **headers}, method="GET")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return False, f"couldn't list models ({exc.code}): {detail}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, f"couldn't list models: {exc}"
    items = data.get("data") or []
    return True, sorted(({"id": m["id"], "label": m["id"]} for m in items if m.get("id")),
                        key=lambda m: m["id"])


def _list_models_anthropic(*, headers: dict) -> tuple[bool, list[dict] | str]:
    try:
        data = _post_json("https://api.anthropic.com/v1/models?limit=1000", None,
                          headers={"Accept": "application/json", **headers}, method="GET")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return False, f"couldn't list models ({exc.code}): {detail}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, f"couldn't list models: {exc}"
    items = data.get("data") or []
    return True, sorted(({"id": m["id"], "label": m.get("display_name") or m["id"]}
                         for m in items if m.get("id")), key=lambda m: m["id"])


def _list_models_copilot(provider: dict) -> tuple[bool, list[dict] | str]:
    # api.githubcopilot.com/models -- undocumented by GitHub, same
    # reverse-engineered-from-VS-Code-Copilot-Chat basis as the rest of
    # this provider's mechanics (see module docstring); the response
    # shape (data[].id/.name) mirrors what copilot-api-style gateways
    # report seeing from VS Code's own model picker.
    token = access_token_for(provider)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", **_COPILOT_HEADERS}
    try:
        data = _post_json("https://api.githubcopilot.com/models", None, headers=headers, method="GET")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return False, f"couldn't list models ({exc.code}): {detail}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, f"couldn't list models: {exc}"
    items = data.get("data") or []
    return True, sorted(({"id": m["id"], "label": m.get("name") or m["id"]}
                         for m in items if m.get("id")), key=lambda m: m["id"])


# ── Startup migration (2026-09-30) ─────────────────────────────────────

def migrate_from_env(workspace_id: int, admin_user_id: int) -> None:
    """Called once at boot (see server.py's startup sequence), after
    store.init() has already rebuilt `models` into its new shape with
    every row's provider_id left NULL. If OPENROUTER_API_KEY is set and
    no provider exists yet, auto-create an OpenRouter provider from it and
    point every unlinked model at it -- the upgrade path for the instance
    that's been running on the old env-only key all along. Does nothing
    on a fresh install with no env key: zero providers, zero models,
    surfaced honestly rather than assuming OpenRouter the way the old
    code did."""
    if store.read(lambda c: c.execute("SELECT 1 FROM providers LIMIT 1").fetchone()):
        return
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return
    provider_id = store.write(lambda c: c.execute(
        "INSERT INTO providers(workspace_id, type, label, status, api_key_enc, enabled, "
        "created_ts, created_by) VALUES (?,?,?,?,?,1,?,?)",
        (workspace_id, "openrouter", "OpenRouter (migrated)", "connected", crypto.encrypt(key),
         time.time(), admin_user_id)).lastrowid)
    store.write(lambda c: c.execute(
        "UPDATE models SET provider_id=? WHERE provider_id IS NULL", (provider_id,)))
    # Raw read against the settings table, not config.get() -- config.py's
    # own spec entry for the setting this replaces (default_model_slug) is
    # removed as part of this same change, so going through config.get()
    # here would KeyError. This one-time read is the only place that old
    # value still matters.
    old_row = store.read(lambda c: c.execute(
        "SELECT v FROM settings WHERE scope='workspace' AND scope_id=? AND key='default_model_slug'",
        (workspace_id,)).fetchone())
    old_default = old_row["v"] if old_row else None
    if old_default:
        row = store.read(lambda c: c.execute(
            "SELECT id FROM models WHERE model_name=? AND provider_id=?",
            (old_default, provider_id)).fetchone())
        if row:
            store.write(lambda c: c.execute(
                "INSERT OR REPLACE INTO model_chain(workspace_id, model_id, priority) VALUES (?,?,0)",
                (workspace_id, row["id"])))

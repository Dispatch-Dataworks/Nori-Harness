# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Connected third-party accounts (Gmail/Outlook/Outlook Contacts/Google
Calendar/Google Contacts/Google Drive/OneDrive work+personal/SharePoint/
Skylight) -- the only module with raw SQL against
`connected_accounts`. Tokens encrypted at rest via crypto.py, same as the
sub-agent roster's API keys -- if anything these are more sensitive,
since they're the live keys to someone's real mailbox. The actual OAuth
exchange lives in oauth.py; this module owns storage AND (2026-09-18) is
the one place every authenticated call to a connected provider goes
through -- authed_request() -- so token refresh and error legibility
(expired/revoked/scope-missing/API-disabled) are handled once, not
reimplemented per domain module the way email_calendar.py originally did.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import crypto
import oauth
import store

PROVIDERS = ("gmail", "outlook", "outlook_contacts", "google_calendar", "google_contacts",
            "google_drive", "onedrive_work", "onedrive_personal", "sharepoint", "skylight")

# Refresh proactively once this close to expiry, not only after the fact
# -- a call that starts 5s before expiry and takes 6s would otherwise hit
# a live 401 mid-flight for no real reason.
_EXPIRY_SAFETY_MARGIN_S = 60


def get(user_id: int, provider: str) -> dict | None:
    r = store.read(lambda c: c.execute(
        "SELECT * FROM connected_accounts WHERE user_id=? AND provider=?",
        (user_id, provider)).fetchone())
    return dict(r) if r else None


def list_for_user(user_id: int) -> dict[str, dict]:
    """Every known provider, even ones with no row yet (reported as
    not_connected) -- so a settings page always has a consistent row to
    render per provider, first connection or not."""
    existing = {r["provider"]: dict(r) for r in store.read(lambda c: c.execute(
        "SELECT * FROM connected_accounts WHERE user_id=?", (user_id,)).fetchall())}
    return {p: existing.get(p, {"provider": p, "status": "not_connected"}) for p in PROVIDERS}


def start_pending(user_id: int, provider: str, state: str) -> None:
    def _w(c):
        row = c.execute("SELECT 1 FROM connected_accounts WHERE user_id=? AND provider=?",
                        (user_id, provider)).fetchone()
        if row:
            c.execute("UPDATE connected_accounts SET oauth_state=? WHERE user_id=? AND provider=?",
                     (state, user_id, provider))
        else:
            c.execute("INSERT INTO connected_accounts(user_id, provider, status, oauth_state) "
                     "VALUES (?,?,'not_connected',?)", (user_id, provider, state))
    store.write(_w)


def check_state(user_id: int, provider: str, state: str) -> bool:
    if not state:
        return False
    row = get(user_id, provider)
    return bool(row) and row.get("oauth_state") == state


def store_tokens(user_id: int, provider: str, access_token: str, refresh_token: str | None,
                expires_in: int | None, meta: dict | None = None) -> None:
    now = time.time()
    expires_ts = (now + expires_in) if expires_in else None
    store.write(lambda c: c.execute(
        "UPDATE connected_accounts SET status='connected', access_token_enc=?, refresh_token_enc=?, "
        "token_expires_ts=?, connected_ts=?, meta=?, oauth_state=NULL WHERE user_id=? AND provider=?",
        (crypto.encrypt(access_token), crypto.encrypt(refresh_token) if refresh_token else None,
         expires_ts, now, json.dumps(meta or {}), user_id, provider)))


def disconnect(user_id: int, provider: str) -> None:
    store.write(lambda c: c.execute(
        "UPDATE connected_accounts SET status='not_connected', access_token_enc=NULL, "
        "refresh_token_enc=NULL, token_expires_ts=NULL, connected_ts=NULL, meta=NULL, oauth_state=NULL "
        "WHERE user_id=? AND provider=?", (user_id, provider)))


def real_access_token(row: dict) -> str | None:
    return crypto.decrypt(row["access_token_enc"]) if row.get("access_token_enc") else None


def real_refresh_token(row: dict) -> str | None:
    return crypto.decrypt(row["refresh_token_enc"]) if row.get("refresh_token_enc") else None


def label(provider: str) -> str:
    return oauth.PROVIDERS.get(provider, {}).get("label", provider)


def update_access_token(user_id: int, provider: str, access_token: str, expires_in: int | None) -> None:
    """A successful refresh's own write -- deliberately separate from
    store_tokens(), which always sets refresh_token_enc (to NULL if none
    is passed). A refresh response almost never includes a new refresh
    token -- Google's doesn't; the original stays valid -- so reusing
    store_tokens() here would silently wipe out the one good refresh
    token this whole mechanism depends on. Also clears needs_reconnect
    back to connected: a working refresh is real proof access is fine
    again, regardless of what the status said before this call."""
    now = time.time()
    expires_ts = (now + expires_in) if expires_in else None
    store.write(lambda c: c.execute(
        "UPDATE connected_accounts SET status='connected', access_token_enc=?, token_expires_ts=? "
        "WHERE user_id=? AND provider=?",
        (crypto.encrypt(access_token), expires_ts, user_id, provider)))


def mark_needs_reconnect(user_id: int, provider: str, reason: str) -> None:
    """Distinct from 'not_connected' (2026-09-18) -- that status means no
    one ever connected this provider; this one means a real connection
    went bad (refresh token revoked/expired, or the API itself rejected
    an otherwise-fresh-looking token with a 401) and needs a human to
    redo the consent screen, not just wait. The reason rides in `meta`
    so the settings page can show exactly what went wrong instead of a
    bare "reconnect" with no context -- the confabulation risk this
    whole feature is guarding against is a LATER turn inventing a
    plausible-sounding explanation for why email suddenly stopped
    working; a real stored reason is what stops that."""
    store.write(lambda c: c.execute(
        "UPDATE connected_accounts SET status='needs_reconnect', meta=? WHERE user_id=? AND provider=?",
        (json.dumps({"needs_reconnect_reason": reason[:300]}), user_id, provider)))


# Google's own fixed clock for a "Testing"-status OAuth consent screen: a
# refresh token issued under it dies after exactly 7 days, regardless of use
# (see docs/google-and-microsoft.md). Bounds for the heuristic below -- not
# proof, since Google returns the identical invalid_grant either way, but a
# real, named, checkable possibility beats a bare "reconnect required."
_TESTING_EXPIRY_MIN_AGE_S = 7 * 86400
_TESTING_EXPIRY_MAX_AGE_S = 90 * 86400  # generous; past this, age stops being useful evidence
# "gmail" doesn't start with "google" -- the other three Google providers do.
_GOOGLE_PROVIDERS = ("gmail", "google_calendar", "google_contacts", "google_drive")


def reconnect_diagnosis(provider: str, info: dict) -> str:
    """The stored provider error (mark_needs_reconnect's own reason), plus
    -- only when the shape actually matches -- a plain-language likely
    cause layered on top. Shared by the settings page and the proactive
    ping signal (below) so the two surfaces never disagree about the same
    failure; never invented when the evidence doesn't fit."""
    reason = None
    try:
        reason = json.loads(info["meta"]).get("needs_reconnect_reason") if info.get("meta") else None
    except Exception:  # noqa: BLE001 -- malformed meta must not crash whatever's asking, just lose the extra detail
        reason = None
    if not reason:
        return ""
    connected_ts = info.get("connected_ts")
    age = (time.time() - connected_ts) if connected_ts else None
    if ("invalid_grant" in reason.lower() and provider in _GOOGLE_PROVIDERS
            and age is not None and _TESTING_EXPIRY_MIN_AGE_S <= age <= _TESTING_EXPIRY_MAX_AGE_S):
        return (f"{reason} -- likely your Google Cloud OAuth app is still in \"Testing\" status "
                f"(refresh tokens expire after exactly 7 days there, regardless of use); "
                f"see docs/google-and-microsoft.md for the fix")
    return reason


def scheduler_signal(user_id: int) -> "tuple[str, str] | None":
    """A connected account that's actually gone bad -- needs_reconnect,
    not merely never-connected -- surfaced proactively rather than sitting
    silent until someone happens to open Settings > Connected accounts.
    Real incident behind this (2026-09-27): four of the operator's own
    Google accounts died the same week and nothing ever told him; the
    settings page had the real reason the whole time, nobody was pointed
    at it.

    Zero cost, by construction: reads only the cached status
    connected_accounts already maintains from a real failed refresh/API
    call (mark_needs_reconnect) -- never a fresh network probe, never a
    model call, so a dead account costs nothing extra to keep reporting
    on. Per-provider try/except (same reasoning email_calendar.py's own
    calendar_scheduler_signal already uses) so one malformed row can't
    take out every other account's check or the rest of the ping cycle.

    Dedupes on the SORTED set of currently-broken provider names, not a
    bare key -- the same set of broken accounts stays cooled down (no
    repeat nagging for accounts that are still exactly as broken as last
    time), but a genuinely new failure -- a different provider breaking,
    or the set changing after a reconnect -- reads as a fresh candidate
    rather than being silently swallowed by an old cooldown."""
    try:
        accounts = list_for_user(user_id)
    except Exception as exc:  # noqa: BLE001 -- a signal check must never itself crash the ping cycle
        print(f"connected_accounts.scheduler_signal: user {user_id}: {exc}", flush=True)
        return None
    broken = []
    for provider, info in accounts.items():
        try:
            if info.get("status") == "needs_reconnect":
                broken.append((provider, info))
        except Exception as exc:  # noqa: BLE001
            print(f"connected_accounts.scheduler_signal: user {user_id} ({provider}): {exc}", flush=True)
    if not broken:
        return None

    broken.sort(key=lambda pair: pair[0])
    parts = []
    for provider, info in broken:
        diagnosis = reconnect_diagnosis(provider, info)
        parts.append(f"{label(provider)}{f' ({diagnosis})' if diagnosis else ''}")

    dedup_token = ",".join(p for p, _ in broken)
    message = ("one or more connected accounts need reconnecting: " + "; ".join(parts) +
              " -- reconnect each from Settings > Connected accounts.")
    return message, dedup_token


import scheduler  # local-at-module-bottom on purpose: registers this module's signal once, at import
scheduler.register_signal(scheduler_signal, key="account_needs_reconnect",
                          label="A connected account needs reconnecting",
                          tier="routine", cooldown_seconds=24 * 3600)


def get_valid_access_token(user_id: int, provider: str) -> dict:
    """The one place a tool implementation gets a token it can actually
    use -- returns {"ok": True, "access_token"} or {"ok": False, "error"}
    with a SPECIFIC reason: never connected, expired with no refresh
    token to renew it, refresh failed because access was revoked/expired
    (marks needs_reconnect so this doesn't repeat the same doomed refresh
    attempt every call), or a transient failure (network/timeout -- left
    as 'connected' since the stored refresh token might still be good
    next time). Never silently returns a stale token and never invents a
    reason for a failure it didn't specifically diagnose."""
    row = get(user_id, provider)
    if not row or row["status"] == "not_connected":
        return {"ok": False, "error": f"{label(provider)} isn't connected for this account yet -- "
                                      f"connect it at /settings?tab=accounts"}
    if row["status"] == "needs_reconnect":
        reason = (json.loads(row["meta"]).get("needs_reconnect_reason") if row.get("meta") else None)
        detail = f" ({reason})" if reason else ""
        return {"ok": False, "error": f"{label(provider)} needs to be reconnected{detail} -- "
                                      f"reconnect it at /settings?tab=accounts"}
    expires_ts = row.get("token_expires_ts")
    if expires_ts is None or expires_ts - time.time() > _EXPIRY_SAFETY_MARGIN_S:
        return {"ok": True, "access_token": real_access_token(row)}
    refresh = real_refresh_token(row)
    if not refresh:
        mark_needs_reconnect(user_id, provider, "access token expired and no refresh token was stored")
        return {"ok": False, "error": f"{label(provider)}'s access token has expired and there's no "
                                      f"refresh token to renew it -- reconnect at /settings?tab=accounts"}
    result = oauth.refresh_token(provider, refresh)
    if result.get("ok"):
        update_access_token(user_id, provider, result["access_token"], result.get("expires_in"))
        return {"ok": True, "access_token": result["access_token"]}
    err = result.get("error", "")
    if "invalid_grant" in err or "invalid_request" in err:
        # invalid_grant is Google/Microsoft's real, specific signal for
        # "this refresh token is revoked or expired" -- not a guess from
        # the HTTP status alone. Retrying with the same refresh token
        # would just fail identically, so this needs a human, now.
        mark_needs_reconnect(user_id, provider, err)
        return {"ok": False, "error": f"{label(provider)} access was revoked or expired ({err}) -- "
                                      f"reconnect at /settings?tab=accounts"}
    return {"ok": False, "error": f"couldn't refresh the {label(provider)} token right now: {err} -- "
                                  f"try again shortly"}


def authed_request(user_id: int, provider: str, url: str, *, method: str = "GET",
                   body: dict | bytes | None = None, content_type: str | None = None,
                   raw_response: bool = False) -> dict:
    """The one place every real HTTP call to a connected provider's API
    goes through (email_calendar.py, contacts.py, drive.py) -- refreshes
    the token first if needed (get_valid_access_token), then makes the
    call, then diagnoses a failure specifically rather than wrapping it
    in one generic message: a 401 despite a locally-fresh-looking token
    means access was revoked on the provider's side since the last
    refresh (marks needs_reconnect, same as a failed refresh); a 403 is
    either a missing scope or the API not being enabled on the project,
    and Google/Microsoft's own error message says which; anything else
    still carries the provider's real error text, never just an HTTP
    code.

    body/raw_response (2026-09-18, Drive) -- Drive's own upload
    (multipart/related) and download (files.get?alt=media, files.export)
    endpoints are the first callers that aren't plain JSON in and out:
    body=bytes with an explicit content_type sends it as-is instead of
    JSON-encoding it; raw_response=True skips json.loads() on the way
    back and returns the real bytes. Every existing caller passes a dict
    body and reads a dict back, unchanged.

    PATCH/PUT (2026-09-18, Microsoft set) -- Graph uses PATCH for real
    partial updates (e.g. a message's categories) and PUT for OneDrive's
    simple-upload content endpoint, where Google's equivalents (Gmail's
    modify, Drive's multipart upload) both happened to be POST. Same
    body-encoding rules as POST, just a different verb on the wire --
    there's nothing method-specific about JSON-encoding a dict body or
    passing raw bytes through unchanged."""
    tok = get_valid_access_token(user_id, provider)
    if not tok.get("ok"):
        return {"ok": False, "error": tok["error"]}
    headers = {"Authorization": f"Bearer {tok['access_token']}"}
    data = None
    if method in ("POST", "PATCH", "PUT"):
        if isinstance(body, bytes):
            data = body
            headers["Content-Type"] = content_type or "application/octet-stream"
        else:
            data = json.dumps(body or {}).encode("utf-8")
            headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return {"ok": True, "data": raw if raw_response else json.loads(raw.decode("utf-8"))}
    except urllib.error.HTTPError as exc:
        detail = oauth.extract_error_detail(exc.read())
        if exc.code == 401:
            mark_needs_reconnect(user_id, provider, detail)
            return {"ok": False, "error": f"{label(provider)} access was revoked or is no longer valid "
                                          f"({detail}) -- reconnect at /settings?tab=accounts",
                    "http_status": exc.code}
        if exc.code == 403:
            return {"ok": False, "error": f"{label(provider)} refused this request ({detail}) -- check "
                                          f"that the API is enabled on the Google Cloud project and that "
                                          f"the connected scope actually covers this action",
                    "http_status": exc.code}
        # http_status (2026-09-19, integration_health.py's own need) --
        # additive only, every existing caller here already reads just
        # "ok"/"error" and ignores unknown keys; this lets a health check
        # classify rate_limited (429) vs. a genuine server error without
        # regexing the prose message, which never carried a code before.
        return {"ok": False, "error": f"{label(provider)} request failed ({exc.code}): {detail}",
                "http_status": exc.code}
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"ok": False, "error": f"couldn't reach {label(provider)}: {exc}"}


# ── peer gating for connected-account tools (2026-09-18) ──────────────────
# Proposed, not assumed, per the operator's own instruction: email is his
# private correspondence, held back from any peer-motivated turn entirely
# until he says otherwise; calendar/contacts are lower-stakes and open to
# a peer-motivated turn only once that specific peer is fully trusted --
# the same bar the PACI specification's own "what can they ask you to change"
# already uses for a capability change, reused here for tool visibility
# rather than inventing a second trust axis. Wired as each tool's own
# owner_check (tools.py), which tools.dispatch() already enforces as a
# hard runtime gate, not just a hint to the model about what to offer --
# same mechanism MCP-connection-scoped tools already use.
def peer_blocked(session: dict) -> bool:
    """True (visible/callable) unless this is a peer-motivated turn at
    all -- no trust level is high enough to unlock this one."""
    return not session.get("_peer_context")


def peer_trust_gate(session: dict) -> bool:
    """True for an ordinary turn; for a peer-motivated one, only when
    that peer's own trust_level is 'full' (set on session["_peer_trust"]
    by peers._run_prompted_turn)."""
    if not session.get("_peer_context"):
        return True
    return session.get("_peer_trust") == "full"

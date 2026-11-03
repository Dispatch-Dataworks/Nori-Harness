# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""GitHub App sign-in for infra_github.py (2026-11-02, operator: "My primary account and all the orgs I have access to. Other services do exactly this with a
single connection").

A fine-grained personal token reaches ONE account's repositories. A GitHub App's user token reaches every account and organisation the app is installed on,
and only the repositories each installation was given, with only the permissions both the app and the person have. This module signs in with the device
flow (a code the person enters at github.com/login/device), which needs the app's public client ID and NO secret, for the sign-in and for renewing it.

  * The token lasts 8 hours and is renewed here, under a lock: GitHub rotates the refresh token on every use, so two renewals racing would leave one holding a
    dead token and sign Nori out. The renewal lasts 6 months; after that, or if it is revoked, the sign-in is dropped and the settings page says to sign in again.
  * The only addresses this module talks to are GitHub's two sign-in endpoints (DEVICE_CODE_URL, OAUTH_TOKEN_URL), POST, form-encoded, no Authorization header,
    no redirect followed. infra_github.py stays GET-only; it asks app_token() for the bearer and sends its own requests.
  * The session (access + refresh token, their expiry) is one encrypted credential value, "app_session", like every other credential in infra.py; the device
    code being entered lives only in memory, for at most 15 minutes.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import infra

SERVICE = "github"
TIMEOUT_S = 15
DEVICE_CODE_URL = "https://github.com/login/device/code"
OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
DEVICE_URL = "https://github.com/login/device"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def gh_error(msg: str):
    import infra_github                                   # the same error class the tools already turn into a sentence
    return infra_github.GithubError(msg)


_APP_LOCK = threading.Lock()
_PENDING: dict[int, dict] = {}          # workspace -> the device code being entered (in memory: 15 minutes at most)


def _oauth_post(url: str, params: dict) -> tuple[int, dict]:
    """Third seam: GitHub's own sign-in endpoints. POST, form-encoded, JSON back, no redirect, no Authorization header (the device flow carries no secret)."""
    if url not in (DEVICE_CODE_URL, OAUTH_TOKEN_URL):
        raise gh_error("internal: not a GitHub sign-in address")
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), method="POST",
                                 headers={"Accept": "application/json", "User-Agent": "Nori-infra", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as r:
            raw = r.read(100_000)
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(8000), e.code
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise gh_error(f"could not reach GitHub ({type(exc).__name__})") from None
    try:
        d = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        d = {}
    return status, d if isinstance(d, dict) else {}


def _app_session(ws: int) -> dict:
    raw = infra.credentials(ws, SERVICE).get("app_session") or ""
    try:
        d = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) and d.get("access_token") else {}


def _save_session(ws: int, d: dict, user_id: int | None = None) -> None:
    infra.set_credential_values(ws, SERVICE, {"app_session": json.dumps(d) if d else ""}, user_id)


def _session_from(answer: dict, now: float) -> dict:
    return {"access_token": answer["access_token"], "refresh_token": answer.get("refresh_token") or "",
            "expires_at": now + float(answer["expires_in"]) if answer.get("expires_in") else None,
            "refresh_expires_at": now + float(answer["refresh_token_expires_in"]) if answer.get("refresh_token_expires_in") else None}


def app_token(ws: int) -> str:
    """The app sign-in's current token, renewed first when it is about to expire. "" when there is no sign-in."""
    with _APP_LOCK:
        s = _app_session(ws)
        if not s:
            return ""
        if not s.get("expires_at") or time.time() < float(s["expires_at"]) - 120:
            return s["access_token"]
        cid = (infra.credentials(ws, SERVICE).get("app_client_id") or "").strip()
        if not s.get("refresh_token") or not cid:
            raise gh_error("the GitHub App sign-in has expired: sign in again in Settings > Infrastructure > GitHub")
        status, d = _oauth_post(OAUTH_TOKEN_URL, {"client_id": cid, "grant_type": "refresh_token", "refresh_token": s["refresh_token"]})
        if status != 200 or not d.get("access_token"):
            if d.get("error") in ("bad_refresh_token", "unauthorized", "incorrect_client_credentials") or status in (400, 401):
                _save_session(ws, {})
                infra.log(ws, None, SERVICE, "credentials", "app sign-in ended", None, False, str(d.get("error") or status)[:100])
                raise gh_error("GitHub ended the app sign-in (it was revoked or not used for six months): sign in again in Settings > Infrastructure > GitHub")
            raise gh_error(f"could not renew the GitHub App sign-in (HTTP {status}); try again shortly")
        _save_session(ws, {**s, **_session_from(d, time.time())})
        return d["access_token"]


def app_signed_in(ws: int) -> bool:
    return bool(_app_session(ws))


def ready(ws: int) -> bool:
    return app_signed_in(ws) or bool(infra.credentials(ws, SERVICE).get("token"))


def install_link(ws: int) -> str:
    slug = (infra.credentials(ws, SERVICE).get("app_slug") or "").strip()
    return f"https://github.com/apps/{slug}/installations/new" if slug else ""


def app_start(ws: int, user_id: int | None) -> tuple[bool, str]:
    """The settings button: ask GitHub for a code to enter at github.com/login/device."""
    cid = (infra.credentials(ws, SERVICE).get("app_client_id") or "").strip()
    if not cid:
        return False, "save the GitHub App's client ID first (the app's settings page on GitHub shows it)"
    status, d = _oauth_post(DEVICE_CODE_URL, {"client_id": cid})
    if status != 200 or not d.get("device_code") or not d.get("user_code"):
        why = d.get("error_description") or d.get("error") or f"HTTP {status}"
        if d.get("error") == "device_flow_disabled":
            why = "the app does not allow it: tick Enable Device Flow in the app's settings on GitHub"
        return False, f"GitHub would not start the sign-in: {str(why)[:200]}"
    code = re.sub(r"[^A-Z0-9-]", "", str(d["user_code"]).upper())[:20]
    _PENDING[ws] = {"device_code": d["device_code"], "user_code": code, "interval": int(d.get("interval") or 5),
                    "expires_at": time.time() + int(d.get("expires_in") or 900), "last_poll": 0.0}
    return True, (f"On GitHub, open {DEVICE_URL} and enter the code {code} (good for {int(d.get('expires_in') or 900) // 60} minutes), approve the app, then press "
                  "'finish GitHub App sign-in' here.")


def app_finish(ws: int, user_id: int | None) -> tuple[bool, str]:
    p = _PENDING.get(ws)
    if not p:
        return False, "start with 'sign in with the GitHub App'"
    if time.time() > p["expires_at"]:
        _PENDING.pop(ws, None)
        return False, "that code has expired: start the sign-in again"
    wait = p["last_poll"] + p["interval"] - time.time()
    if wait > 0:
        return False, f"GitHub asks for a few seconds between checks: press finish again in {int(wait) + 1} s"
    cid = (infra.credentials(ws, SERVICE).get("app_client_id") or "").strip()
    p["last_poll"] = time.time()
    status, d = _oauth_post(OAUTH_TOKEN_URL, {"client_id": cid, "device_code": p["device_code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    err = d.get("error")
    if err == "authorization_pending":
        return False, f"GitHub has not seen the code yet: enter {p['user_code']} at {DEVICE_URL}, approve, then press finish again"
    if err == "slow_down":
        p["interval"] = int(d.get("interval") or p["interval"] + 5)
        return False, f"GitHub asks to wait a little: press finish again in {p['interval']} s"
    if err in ("expired_token", "token_expired"):
        _PENDING.pop(ws, None)
        return False, "that code has expired: start the sign-in again"
    if err == "access_denied":
        _PENDING.pop(ws, None)
        return False, "the sign-in was cancelled on GitHub"
    if status != 200 or not d.get("access_token"):
        return False, f"GitHub would not finish the sign-in: {str(d.get('error_description') or err or status)[:200]}"
    _PENDING.pop(ws, None)
    with _APP_LOCK:
        _save_session(ws, _session_from(d, time.time()), user_id)
    infra.log(ws, user_id, SERVICE, "credentials", "app sign-in", None, True, "")
    import infra_github                                   # here, not at the top: infra_github imports this module
    res = infra_github.test(ws)
    return True, "Signed in with the GitHub App. " + res["detail"]


def app_forget(ws: int, user_id: int | None) -> tuple[bool, str]:
    if not app_signed_in(ws):
        return False, "there is no GitHub App sign-in"
    with _APP_LOCK:
        _save_session(ws, {}, user_id)
    return True, ("signed out of the GitHub App here" + (" (the personal access token is used again)" if infra.credentials(ws, SERVICE).get("token") else "")
                  + ". To end it on GitHub too: Settings > Applications > Authorized GitHub Apps > Revoke.")

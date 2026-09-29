# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Generic OAuth2 authorization-code flow -- the connect/callback
mechanics, provider-agnostic. Each provider's endpoints and scope live in
PROVIDERS; client_id/secret come from the operator's own .env, never
assumed or checked in. A provider with no client_id/secret configured is
honestly reported as "not configured" rather than attempting a flow that
would just fail -- this is the boundary this build stops at: the operator
registers their own OAuth app with each provider and supplies the
resulting credentials; everything up to that point is real and ready.

Gmail/Outlook/Google Calendar's endpoints below are their real, standard,
publicly documented OAuth2 endpoints. Skylight's are NOT -- there's no
verified public OAuth spec for Skylight Calendar available here, and it
may not even use OAuth at all. Its entry is deliberately left with no
URLs rather than plausible-looking invented ones -- the operator will
need to confirm what Skylight's real integration mechanism actually is.
"""
from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

PROVIDERS: dict[str, dict] = {
    "gmail": {
        "label": "Gmail",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        # gmail.modify (2026-09-18, widened from gmail.readonly -- he wants
        # drafting and label/move too). Checked against Google's own
        # method-scope reference, not assumed: modify is a strict superset
        # of readonly (every read method modify covers too), so this is
        # ONE scope, not two -- requesting readonly alongside it would be
        # redundant, not narrower. Real, load-bearing limits on what this
        # scope actually excludes: it does NOT exclude sending (drafts.
        # send/messages.send both accept it -- there is no Gmail scope
        # that permits draft creation but not sending; verified, not
        # assumed, see email_calendar.py's create_draft for where the
        # actual no-send guarantee lives). It DOES exclude permanent,
        # bypass-Trash deletion (messages.delete/threads.delete need the
        # full mail.google.com scope) -- the one guarantee Google actually
        # enforces here rather than us.
        "scope": "https://www.googleapis.com/auth/gmail.modify",
        # One shared Google Cloud OAuth client covers all four Google
        # providers here (2026-09-18, his own env naming, better than the
        # per-provider names originally given -- one real app registered
        # once, not four separate ones) -- GOOGLE_CLIENT_ID/SECRET, not
        # NORI_-prefixed. Every Google entry below points at the same pair.
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
        "extra_auth_params": {"access_type": "offline", "prompt": "consent"},
    },
    "google_calendar": {
        "label": "Google Calendar",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        # calendar.events (2026-09-18), not the broader "calendar" scope --
        # read/write on events only, not calendar-list management. The
        # least-privilege scope that still covers create_calendar_event.
        "scope": "https://www.googleapis.com/auth/calendar.events",
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
        "extra_auth_params": {"access_type": "offline", "prompt": "consent"},
    },
    "google_contacts": {
        "label": "Google Contacts",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        # Widened from contacts.readonly (2026-09-18, he wants edit too).
        # No read+write-but-no-delete People API scope exists -- this is
        # the same shape as every other Google scope here: broader than
        # the read-only original, and technically permits deleting a
        # contact too. No contact-delete tool is registered anywhere in
        # contacts.py; that's the guarantee, same as everywhere else.
        "scope": "https://www.googleapis.com/auth/contacts",
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
        "extra_auth_params": {"access_type": "offline", "prompt": "consent"},
    },
    "google_drive": {
        "label": "Google Drive",
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        # Full drive scope, not drive.file -- checked specifically (2026-
        # 09-18): drive.file is narrower but only ever sees files THIS app
        # created or he explicitly picked via a file-open dialog neither
        # of which describes "read my existing Drive," so it wouldn't do
        # what he asked. Also checked: drive.file permits files.delete
        # exactly as much as the full scope does -- narrowing scope buys
        # no delete-safety here, so there's no reason to take the scope
        # that hides his existing files instead of the one that doesn't.
        # No write/delete tool exists in drive.py at all -- see that
        # module's own docstring for why, and don't add one casually.
        "scope": "https://www.googleapis.com/auth/drive",
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
        "extra_auth_params": {"access_type": "offline", "prompt": "consent"},
    },
    "outlook": {
        "label": "Outlook",
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        # Widened from Mail.Read Calendars.Read (2026-09-18, real gap found
        # while building the rest of the Microsoft set: create_calendar_event
        # already existed and already POSTed to Outlook's write endpoint, but
        # the granted scope was read-only -- that call would have 403'd the
        # moment Outlook was actually connected. Mail.ReadWrite deliberately
        # does NOT include Mail.Send -- checked against Graph's own
        # permissions reference: sending mail is a separate permission
        # entirely, so (unlike Gmail, where gmail.modify technically also
        # permits sending and the no-send guarantee is code-only) omitting
        # Mail.Send here is a REAL scope-level guarantee, not just "no send
        # function exists." Graph does NOT scope-separate soft vs. permanent
        # delete the way Google does, though -- Mail.ReadWrite covers both,
        # so "no permanent delete" for Outlook mail is still a code-level
        # guarantee only (no delete tool built), same caveat as everywhere
        # else here.
        "scope": "offline_access Mail.ReadWrite Calendars.ReadWrite",
        "client_id_env": "NORI_OUTLOOK_CLIENT_ID",
        "client_secret_env": "NORI_OUTLOOK_CLIENT_SECRET",
        "extra_auth_params": {},
    },
    "outlook_contacts": {
        "label": "Outlook Contacts",
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        # Contacts.ReadWrite (2026-09-18, first build) -- same shape as
        # Google Contacts: broader than read-only (no read+write-but-no-
        # delete Graph permission exists either), no write/delete tool
        # registered in contacts.py -- that's the real guarantee, code not
        # scope, same as Google's.
        "scope": "offline_access Contacts.ReadWrite",
        "client_id_env": "NORI_OUTLOOK_CLIENT_ID",
        "client_secret_env": "NORI_OUTLOOK_CLIENT_SECRET",
        "extra_auth_params": {},
    },
    "onedrive_work": {
        "label": "OneDrive (work)",
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        # Files.ReadWrite (2026-09-18) -- same shape as Google Drive's full
        # `drive` scope: technically permits write/delete, no write or
        # delete TOOL exists in drive.py for OneDrive either -- read is a
        # tool, write is a UI action only (files_page/files_onedrive_post),
        # same precedent the operator set for Google Drive, applied here
        # without a separate decision since he said to follow it unless
        # there's a reason not to, and there isn't one.
        "scope": "offline_access Files.ReadWrite",
        "client_id_env": "NORI_OUTLOOK_CLIENT_ID",
        "client_secret_env": "NORI_OUTLOOK_CLIENT_SECRET",
        "extra_auth_params": {},
    },
    "onedrive_personal": {
        "label": "OneDrive (personal)",
        # /consumers/, not /common/ (2026-09-18, operator's own choice: a
        # SEPARATE app registration for the personal Microsoft account,
        # scoped to personal accounts only) -- matches the account type
        # that app is registered for. Real, separate credential pair below,
        # not the work tenant's.
        "auth_url": "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        "scope": "offline_access Files.ReadWrite",
        "client_id_env": "NORI_OUTLOOK_PERSONAL_CLIENT_ID",
        "client_secret_env": "NORI_OUTLOOK_PERSONAL_CLIENT_SECRET",
        "extra_auth_params": {},
    },
    "sharepoint": {
        "label": "SharePoint",
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        # Sites.ReadWrite.All (2026-09-18, operator's own explicit choice,
        # made with the tradeoff stated plainly first): he wants every site
        # in the tenant reachable, not a per-site allowlist, so Sites.Selected
        # -- the one permission Graph itself enforces per-site -- doesn't fit
        # what he asked for. ReadWrite.All is requested alone, not alongside
        # Sites.Read.All, since it's a strict superset (same "one scope, not
        # two" reasoning as gmail.modify above) -- checked against Graph's
        # own reference, not assumed. There is no tenant-wide Graph
        # permission that grants edit but not delete, so "no permanent
        # delete" for SharePoint files is a code-level guarantee only (no
        # delete tool exists in sharepoint.py) -- same caveat as OneDrive/
        # Drive, just with a broader blast radius given tenant-wide scope.
        "scope": "offline_access Sites.ReadWrite.All",
        "client_id_env": "NORI_OUTLOOK_CLIENT_ID",
        "client_secret_env": "NORI_OUTLOOK_CLIENT_SECRET",
        "extra_auth_params": {},
    },
    "skylight": {
        "label": "Skylight Calendar",
        "auth_url": None,   # UNCONFIRMED -- see module docstring; operator must confirm the real mechanism
        "token_url": None,
        "scope": "",
        "client_id_env": "NORI_SKYLIGHT_CLIENT_ID",
        "client_secret_env": "NORI_SKYLIGHT_CLIENT_SECRET",
        "extra_auth_params": {},
    },
}


def is_configured(provider: str) -> bool:
    p = PROVIDERS.get(provider)
    if not p or not p["auth_url"]:
        return False
    return bool(os.environ.get(p["client_id_env"]) and os.environ.get(p["client_secret_env"]))


def new_state() -> str:
    return secrets.token_urlsafe(24)


def authorize_url(provider: str, state: str, redirect_uri: str) -> str | None:
    p = PROVIDERS.get(provider)
    if not p or not is_configured(provider):
        return None
    params = {
        "client_id": os.environ[p["client_id_env"]],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": p["scope"],
        "state": state,
        **p["extra_auth_params"],
    }
    return f"{p['auth_url']}?{urllib.parse.urlencode(params)}"


def extract_error_detail(body: bytes) -> str:
    """The specific reason from a failed Google/Microsoft response body,
    not just the HTTP status -- covers both shapes seen in practice: an
    OAuth token-endpoint error ({"error":"invalid_grant","error_
    description":"..."}) and a real API error ({"error":{"code":403,
    "message":"...","status":"..."}}). Falls back to the raw body, then
    to a fixed string, rather than ever returning an empty detail --
    "the specific wrong thing" is the whole point of calling this
    instead of just str(exc) (2026-09-18, operator's own ask: expired
    token/revoked access/missing scope/API not enabled all need their
    own distinguishable message, not one generic wrapper)."""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        text = body.decode("utf-8", "replace").strip()
        return text[:300] if text else "empty error response"
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or json.dumps(err))[:300]
    if isinstance(err, str):
        desc = data.get("error_description")
        return f"{err}: {desc}"[:300] if desc else err[:300]
    return json.dumps(data)[:300]


def _post_token_request(p: dict, body: dict) -> dict:
    """Shared by exchange_code/refresh_token -- same endpoint, same error
    handling, different grant. HTTPError is read for its real body before
    anything else, since urlopen's own exception text (e.g. "HTTP Error
    400: Bad Request") throws away exactly the detail this exists to
    surface."""
    encoded = urllib.parse.urlencode(body).encode()
    try:
        req = urllib.request.Request(p["token_url"], data=encoded, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": extract_error_detail(exc.read())}
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"ok": False, "error": str(exc)[:300]}
    if "error" in data:
        desc = data.get("error_description")
        detail = f"{data['error']}: {desc}" if desc else str(data["error"])
        return {"ok": False, "error": detail[:300]}
    return {"ok": True, "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"), "expires_in": data.get("expires_in")}


def exchange_code(provider: str, code: str, redirect_uri: str) -> dict:
    """A real HTTP call to the provider's own token endpoint -- no mock
    path; there's nothing worth faking here. Returns
    {"ok": True, "access_token", "refresh_token", "expires_in"} or
    {"ok": False, "error"}."""
    p = PROVIDERS.get(provider)
    if not p or not is_configured(provider):
        return {"ok": False, "error": "provider not configured"}
    return _post_token_request(p, {
        "client_id": os.environ[p["client_id_env"]],
        "client_secret": os.environ[p["client_secret_env"]],
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })


def refresh_token(provider: str, refresh_token_value: str) -> dict:
    """The initial exchange's own missing counterpart (2026-09-18) --
    connected_accounts.py already stored a refresh_token from day one
    (store_tokens has always taken one), nothing ever called this. Same
    return shape as exchange_code, minus redirect_uri (not part of this
    grant type); "refresh_token" in the result is almost always absent --
    Google doesn't reissue one on refresh, the original stays valid, so
    callers must keep the one they already have rather than overwrite it
    with this result's (usually missing) one."""
    p = PROVIDERS.get(provider)
    if not p or not is_configured(provider):
        return {"ok": False, "error": "provider not configured"}
    return _post_token_request(p, {
        "client_id": os.environ[p["client_id_env"]],
        "client_secret": os.environ[p["client_secret_env"]],
        "refresh_token": refresh_token_value,
        "grant_type": "refresh_token",
    })

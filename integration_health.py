# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Integration health checks -- extends the PACI specification's own §4.1 liveness
pattern (see peers.py/paci.py) to every OTHER outward-facing integration
Nori has: Home Assistant, Tavily, each connected Google/Microsoft account,
and each connected MCP server. Built for the same reason PACI's health
check was: a channel failing silently is worse than one that fails loudly,
and "she invented a reason it wasn't working" -- the tracked confabulation
pattern -- is the direct failure mode this closes -- give her a
real, cheap, cached answer to "is this actually connected right now"
instead of a reason to guess.

Same posture as PACI's own health check: cheap, real, no model call, ever
-- a connectivity/auth probe, not an LLM round trip. Extended to a wider
vocabulary than PACI's five states, because these integrations fail in
more specific, more common ways than a peer handshake does:
  - healthy         -- the probe succeeded
  - not_configured  -- no credentials/connection exist at all; NOT a
                       failure, the operator just hasn't set this one up
  - unreachable     -- network/DNS/timeout; nothing usable answered
  - auth_expired    -- a token/key that used to work has stopped (401,
                       a revoked/expired refresh token, a wrong API key)
  - scope_missing   -- authenticated fine, refused for lacking a
                       permission/scope (403, named as such)
  - api_disabled    -- authenticated fine, refused because the API
                       itself isn't turned on for the project/tenant
  - rate_limited    -- 429
  - error           -- anything else; the real detail is always
                       attached, never collapsed to "something's wrong"
  - unknown         -- configured, but never actually checked yet
                       (Tavily's own live probe, deliberately -- below)

Every check function returns {"status", "detail", "explain"}. detail is
the raw, technical text a STRANGER's admin could act on (a wrong Tavily
key reads "web search failed (401): Unauthorized", not "search is
broken") -- same "legible to a stranger" bar PACI's own §4.1 states
explicitly, because this, like PACI, ships as part of the public
harness. explain is a SEPARATE, short thing (2026-09-19, the operator's
own follow-up): what the status actually means and what would fix it, in
Nori's own voice, for her to relay directly -- "not configured" isn't a
fault, and she should be able to say so plainly rather than "search is
unavailable," and "your Outlook connection expired, reconnect it" beats
"I can't reach your email." This is the real antidote to the
confabulation pattern this whole feature exists to close: she told him
a photo hadn't arrived when the true answer was a toggle he'd never
switched on. Given a real, specific reason instead of a bare status
code, she has no need to invent one.

Written in Nori's own agent register, not softened (2026-09-19, standing
principle): she knows and acts like she's an agent,
so explain text says "my Tavily key isn't configured," names .env,
OAuth, scopes, and settings pages directly, the same way she'd reason
about her own machinery out loud. This is a deliberate persona choice for
Nori specifically, not a universal rule every assistant persona has to
follow.

Cost, not just latency (2026-09-19, a real distinction PACI's peer checks
never had to make): Home Assistant/Google/Microsoft/MCP probes below are
free, metadata-only reads against generous quotas -- cheap in the sense
PACI meant, safe to run on a timer. Tavily is NOT: its only real endpoint
IS a paid search call, so running it automatically every few minutes
would quietly burn a stranger's search-credit budget for a feature that's
supposed to be operational overhead, not usage. Tavily's SCHEDULED check
is therefore configuration-only (a real, free distinction: key present
or not) -- the actual live "does this key work" probe only runs on
demand (the admin's "check now" button, or Nori's own read tool asking
for a fresh one is deliberately NOT offered -- see below). Until someone
explicitly checks it live, Tavily reads 'unknown', not 'healthy' -- an
honest "not yet verified," never a guessed pass.

Scope (2026-09-19, a real simplification, stated here rather than left
implicit): connected_accounts rows are per-user, but every OAuth setup
this app has ever documented (INTEGRATIONS.md) assumes ONE operator doing
the connecting -- this sweep checks the primary admin's own connections,
same "first admin" convention backup.py's own _maybe_run_backup already
uses, not a per-household-member fan-out nobody asked for. Home
Assistant/Tavily/MCP servers are instance-wide already, no user_id
involved at all.
"""
from __future__ import annotations

import time

import accounts
import config
import connected_accounts
import contacts
import drive
import email_calendar
import homeassistant
import mcp_servers
import oauth
import sharepoint
import store
import webtools

STATES = ("healthy", "not_configured", "unreachable", "auth_expired",
          "scope_missing", "api_disabled", "rate_limited", "error", "unknown")

LABELS = {
    "home_assistant": "Home Assistant",
    "tavily": "Tavily (web search)",
    "gmail": "Gmail",
    "google_calendar": "Google Calendar",
    "google_contacts": "Google Contacts",
    "google_drive": "Google Drive",
    "outlook_mail": "Outlook Mail",
    "outlook_calendar": "Outlook Calendar",
    "outlook_contacts": "Outlook Contacts",
    "onedrive_work": "OneDrive (work)",
    "onedrive_personal": "OneDrive (personal)",
    "sharepoint": "SharePoint",
}

# (display key, connected_accounts provider, cheap probe URL) -- GET everywhere. FOUND 2026-09-22 (the operator: "Google tokens in nori don't seem to be refreshing"): refresh was fine
# (connected_accounts.get_valid_access_token was never the problem, proven from the live tokens' own expiry history) but google_calendar and google_contacts were probing
# https://www.googleapis.com/calendar/v3/users/me/calendarList and https://people.googleapis.com/v1/people/me -- standard "identity" endpoints that need a BROADER scope
# (calendar/calendar.readonly, profile) than the app ever requests (calendar.events, contacts) or her real tools ever need. That 403'd every single check and told him to
# reconnect something that, proven the same minute by her own list_calendar_events call, was working fine. A health check that fails on a path the app will never exercise
# is worse than no health check: it makes her tell him to reconnect something that works. So every probe below is now literally the same URL (base + a cheap query) her
# real tool in `email_calendar.py` / `contacts.py` / `drive.py` / `sharepoint.py` actually calls, imported from there rather than retyped, so the two can't drift apart by a
# hand-edited literal; `_REAL_TOOL_MODULES` below and its own test (`tests/test_nori_integration_health.py`) are what catches a NEW probe added the same wrong way again.
# outlook_mail and outlook_calendar share ONE connected_accounts row/token ("outlook" -- Mail.ReadWrite + Calendars.ReadWrite live on the same OAuth grant, see oauth.py) but
# are listed as two checks here, matching how the operator actually thinks about them ("is her mail working" and "is her calendar working" are different questions even
# though one token answers both).
_OAUTH_CHECKS = (
    ("gmail", "gmail", email_calendar._GMAIL_LABELS_URL),  # labels.list takes no maxResults/pageSize param; the list itself is already small and cheap
    ("google_calendar", "google_calendar", email_calendar._GCAL_EVENTS_URL + "?maxResults=1"),
    ("google_contacts", "google_contacts", contacts._PEOPLE_LIST_URL + "?personFields=names&pageSize=1"),
    ("google_drive", "google_drive", drive._DRIVE_FILES_URL + "?pageSize=1&fields=files(id)"),
    ("outlook_mail", "outlook", email_calendar._OUTLOOK_MESSAGES_URL + "?$top=1&$select=id"),
    ("outlook_calendar", "outlook", email_calendar._OUTLOOK_EVENTS_URL + "?$top=1&$select=id"),
    ("outlook_contacts", "outlook_contacts", contacts._OUTLOOK_CONTACTS_URL + "?$top=1&$select=id"),
    ("onedrive_work", "onedrive_work", drive._ONEDRIVE_ROOT_CHILDREN_URL + "?$top=1&$select=id"),
    ("onedrive_personal", "onedrive_personal", drive._ONEDRIVE_ROOT_CHILDREN_URL + "?$top=1&$select=id"),
    ("sharepoint", "sharepoint", sharepoint._SITES_SEARCH_URL + "?search=*&$top=1"),  # Graph's /sites has no bare "list all" verb -- $search is required, same as the real tool
)

# The derived-surface guard (CLAUDE.md's rule, applied here): which module actually owns the real endpoint(s) for each probe above, so a test can check the probe URL is
# really one of THAT module's own real endpoints rather than a hand-typed literal that happens to look right. Every key in _OAUTH_CHECKS must appear here -- checked by
# the same test -- so a probe added later without updating this mapping fails loudly instead of silently going unchecked.
_REAL_TOOL_MODULES = {
    "gmail": email_calendar, "google_calendar": email_calendar, "google_contacts": contacts, "google_drive": drive,
    "outlook_mail": email_calendar, "outlook_calendar": email_calendar, "outlook_contacts": contacts,
    "onedrive_work": drive, "onedrive_personal": drive, "sharepoint": sharepoint,
}


def real_endpoint_urls(mod) -> set:
    """Every real API endpoint the given module actually calls (its own module-level `_..._URL` string constants), base only -- a placeholder like {id} or {site_id} is
    stripped at the brace, so a probe URL is checked against the fixed prefix a templated real call would also share."""
    out = set()
    for name, val in vars(mod).items():
        if name.endswith("_URL") and isinstance(val, str):
            out.add(val.split("{")[0].rstrip("/"))
    return out


def _generic_explain(label: str, status: str, detail: str) -> str:
    """Fallback for a status a specific check function didn't hand-write
    a more actionable line for (mainly the low-stakes ones -- rate
    limits and transient network errors don't need per-integration
    wording, "wait and retry" is the whole fix regardless of which
    integration it is). Still names what happened and what to do about
    it, never a bare label -- just less specifically than the hand-
    written cases below for the states worth being specific about
    (not_configured, auth_expired, scope_missing, api_disabled)."""
    if status == "healthy":
        return f"My {label} connection is working normally."
    if status == "unknown":
        return f"I haven't checked my {label} connection yet."
    if status == "rate_limited":
        return f"{label} is rate-limiting my requests right now -- nothing's broken, worth waiting and retrying."
    if status == "unreachable":
        return f"I can't reach {label} right now -- likely transient, worth retrying shortly."
    if status == "not_configured":
        return f"My {label} integration isn't set up yet."
    return f"{label} returned something I don't have a specific read on: {detail}"[:300]


def _classify_http(status: int | None, detail: str) -> str:
    """Shared classifier for every OAuth-based probe below -- one place
    to read rather than reimplementing per provider. status is the real
    HTTP code when one exists (connected_accounts.authed_request carries
    it as of 2026-09-19, added for exactly this); detail is the
    provider's own real error text, used only to tell scope_missing
    apart from api_disabled on a 403 -- Google's two real failure shapes
    for that code use genuinely different wording (checked against
    oauth.py's own extract_error_detail output, not guessed)."""
    if status is None:
        return "unreachable"
    if status == 401:
        return "auth_expired"
    if status == 429:
        return "rate_limited"
    if status == 403:
        low = detail.lower()
        if any(k in low for k in ("disabled", "has not been used", "not enabled", "accessnotconfigured")):
            return "api_disabled"
        return "scope_missing"  # the far more common 403 shape: insufficient scope/permission
    return "error"


def _probe_oauth(user_id: int, provider: str, url: str) -> dict:
    label = connected_accounts.label(provider)
    if not oauth.is_configured(provider):
        return {"status": "not_configured",
                "detail": f"no OAuth credentials set in .env for {label}",
                "explain": (f"My {label} integration isn't set up on this instance at all -- there's "
                           f"no OAuth app registered for it yet. An admin needs to register one and "
                           f"add its credentials to .env (see INTEGRATIONS.md) before I can connect it.")}
    row = connected_accounts.get(user_id, provider)
    if row is None or row["status"] == "not_connected":
        return {"status": "not_configured", "detail": f"{label} isn't connected yet",
                "explain": (f"My {label} is set up, but nobody's connected it yet -- it can be "
                           f"connected from Settings > Connected accounts.")}
    if row["status"] == "needs_reconnect":
        # connected_accounts.reconnect_diagnosis() -- the same function the
        # settings page and the account_needs_reconnect ping signal both
        # use, so this tool never disagrees with either about the same
        # failure (three surfaces, one source, not three computed
        # independently -- a real divergence found and fixed 2026-09-27).
        reason = connected_accounts.reconnect_diagnosis(provider, row)
        return {"status": "auth_expired", "detail": reason or "needs to be reconnected",
                "explain": (f"My {label} connection has expired or was revoked -- {reason} -- it "
                           f"needs to be reconnected from Settings > Connected accounts before I "
                           f"can use it again." if reason else
                           f"My {label} connection has expired or was revoked -- it needs to be "
                           f"reconnected from Settings > Connected accounts before I can use it again.")}
    result = connected_accounts.authed_request(user_id, provider, url)
    if result.get("ok"):
        return {"status": "healthy", "detail": "probe succeeded", "explain": f"My {label} connection is working normally."}
    status = _classify_http(result.get("http_status"), result.get("error", ""))
    detail = result.get("error", "")
    if status == "auth_expired":
        explain = (f"My {label} connection just expired -- it needs to be reconnected from Settings "
                  f"> Connected accounts before I can use it again.")
    elif status == "scope_missing":
        explain = (f"My {label} connection is missing a permission it needs -- reconnecting it "
                  f"(Settings > Connected accounts) usually re-grants the right scope.")
    elif status == "api_disabled":
        explain = (f"My {label} connection is fine, but the underlying API itself isn't turned on "
                  f"for the project -- an admin needs to enable it (see INTEGRATIONS.md).")
    else:
        explain = _generic_explain(label, status, detail)
    return {"status": status, "detail": detail, "explain": explain}


def _primary_admin() -> dict | None:
    admins = [u for u in accounts.all_active_users() if u["role"] == "admin"]
    return admins[0] if admins else None


def _check_home_assistant() -> dict:
    if not homeassistant.configured():
        return {"status": "not_configured",
                "detail": "HOME_ASSISTANT_URL/HOME_ASSISTANT_API_KEY aren't set in .env",
                "explain": ("My Home Assistant connection isn't set up -- HOME_ASSISTANT_URL and "
                           "HOME_ASSISTANT_API_KEY aren't in .env yet. An admin needs to add both.")}
    ok, status, data = homeassistant._request("/api/")
    if ok:
        return {"status": "healthy", "detail": "API responded",
                "explain": "My Home Assistant connection is working normally."}
    if status in (401, 403):
        return {"status": "auth_expired", "detail": str(data),
                "explain": ("Home Assistant is rejecting my requests -- HOME_ASSISTANT_API_KEY is "
                           "likely wrong or expired. An admin needs to check it in .env.")}
    if status is None:
        return {"status": "unreachable", "detail": str(data),
                "explain": "I can't reach Home Assistant right now -- worth checking it's powered on and reachable on the network."}
    return {"status": "error", "detail": str(data),
            "explain": _generic_explain("Home Assistant", "error", str(data))}


def _check_tavily(*, live: bool) -> dict | None:
    """Config-only unless live=True -- see module docstring on why an
    automatic timer never spends a real search credit. Returns None on
    a non-live sweep with nothing new to report -- the caller then
    leaves whatever's already stored untouched (an earlier on-demand
    result, or 'unknown' if there's never been one) rather than
    overwriting a real result with a guess."""
    if not webtools.configured():
        return {"status": "not_configured", "detail": "TAVILY_API_KEY isn't set in .env",
                "explain": ("My Tavily key isn't configured -- there's no TAVILY_API_KEY in .env, "
                           "so I can't search the web. An admin needs to add one.")}
    if not live:
        return None
    result = webtools.search("nori integration health check", max_results=1)
    if result.get("ok"):
        return {"status": "healthy", "detail": "search probe succeeded",
                "explain": "My Tavily connection is working normally."}
    reason = result.get("reason", "")
    if "(401)" in reason:
        return {"status": "auth_expired", "detail": reason,
                "explain": "My Tavily key looks wrong or has been revoked -- an admin needs to check TAVILY_API_KEY in .env."}
    if "(429)" in reason:
        return {"status": "rate_limited", "detail": reason, "explain": _generic_explain("Tavily", "rate_limited", reason)}
    if reason.startswith("web search failed: ") and "(" not in reason:
        return {"status": "unreachable", "detail": reason, "explain": _generic_explain("Tavily", "unreachable", reason)}
    return {"status": "error", "detail": reason, "explain": _generic_explain("Tavily", "error", reason)}


def _record(key: str, status: str, detail: str, explain: str = "") -> None:
    """Logs the TRANSITION, not just the value -- same reasoning PACI's
    own _record_health documents -- and never as a chat message; this
    must never reach a model on its own, a plain print is the whole
    mechanism. explain (2026-09-19) rides alongside detail as its own
    column -- see module docstring on why the two are kept separate:
    detail is the raw technical text, explain is the short, relayable
    "what it means and what would fix it" Nori's own tool actually
    reads."""
    now = time.time()
    prev_row = store.read(lambda c: c.execute(
        "SELECT last_status FROM integration_health WHERE key=?", (key,)).fetchone())
    prev_status = prev_row["last_status"] if prev_row else None
    store.write(lambda c: c.execute(
        "INSERT INTO integration_health(key, last_status, last_checked_ts, last_detail, last_explain, "
        "status_changed_ts) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET last_status=excluded.last_status, "
        "last_checked_ts=excluded.last_checked_ts, last_detail=excluded.last_detail, "
        "last_explain=excluded.last_explain, "
        "status_changed_ts=CASE WHEN last_status != excluded.last_status THEN excluded.last_checked_ts "
        "ELSE status_changed_ts END",
        (key, status, now, (detail or "")[:2000], (explain or "")[:500], now)))
    if prev_status != status:
        print(f"integration_health: {key} {prev_status!r} -> {status!r}: {(detail or '')[:200]}", flush=True)


def run_all_checks(*, live_tavily: bool = False) -> dict[str, dict]:
    """One full sweep -- every integration, checked now. Called by the
    scheduler tick (live_tavily=False, see module docstring), and the
    admin's own 'check all now' button (live_tavily=True). Nothing else
    calls this -- Nori's own read tool (see tools.py registration below)
    deliberately reads CACHED results only via get_all(), never triggers
    a sweep of its own: asking her a question should never fan out into
    a dozen live network calls, several against someone's real mail and
    calendar, in the middle of a chat turn."""
    results = {}

    ha = _check_home_assistant()
    _record("home_assistant", ha["status"], ha["detail"], ha.get("explain", ""))
    results["home_assistant"] = ha

    tavily = _check_tavily(live=live_tavily)
    if tavily is not None:
        _record("tavily", tavily["status"], tavily["detail"], tavily.get("explain", ""))
        results["tavily"] = tavily

    admin = _primary_admin()
    if admin is not None:
        for key, provider, url in _OAUTH_CHECKS:
            r = _probe_oauth(admin["id"], provider, url)
            _record(key, r["status"], r["detail"], r.get("explain", ""))
            results[key] = r

    for server in mcp_servers.list_all_servers():
        key = f"mcp:{server['id']}"
        r = mcp_servers.check_health(server["id"])
        _record(key, r["status"], r["detail"], r.get("explain", ""))
        results[key] = r

    return results


def health_status(key: str) -> dict:
    """For the settings-page chip and Nori's own tool -- always one of
    STATES, or 'unknown' if this key has never been checked (a brand-new
    MCP connection, or Tavily before its first live check -- see module
    docstring). explain is '' for a never-checked key here -- get_all()
    below fills in a generic one, since only it has the display label
    this function doesn't take."""
    row = store.read(lambda c: c.execute(
        "SELECT * FROM integration_health WHERE key=?", (key,)).fetchone())
    if row is None:
        return {"status": "unknown", "checked_ts": None, "changed_ts": None, "detail": "", "explain": ""}
    return {"status": row["last_status"], "checked_ts": row["last_checked_ts"],
            "changed_ts": row["status_changed_ts"], "detail": row["last_detail"] or "",
            "explain": row["last_explain"] or ""}


def get_all() -> dict[str, dict]:
    """Every known key's current CACHED status (no network call here at
    all) -- for the settings page and for Nori's own read tool. Every
    fixed key is always present, even one never checked yet (reads back
    'unknown', not silently absent -- same 'never omit, always say what
    you know' rule PACI's own health_status already follows). MCP rows
    are generated fresh from mcp_servers.list_all_servers() each call, so
    a renamed or removed connection is never shown under a stale label."""
    def _entry(key: str, label: str) -> dict:
        h = health_status(key)
        if not h["explain"]:
            h["explain"] = _generic_explain(label, h["status"], h["detail"])
        return {"label": label, **h}

    out = {key: _entry(key, label) for key, label in LABELS.items()}
    for server in mcp_servers.list_all_servers():
        key = f"mcp:{server['id']}"
        out[key] = _entry(key, f"MCP: {server['name']}")
    return out


# ── proactive ping signals (2026-09-27) ─────────────────────────────────
# Split by STATE, not by integration -- the taxonomy at the top of this file
# already distinguishes actionable-and-persistent (auth_expired/scope_missing/
# api_disabled) from transient-by-design (rate_limited/unreachable) from
# genuinely unclassified (error/unknown); a ping scheme should follow that
# split rather than invent a parallel one. not_configured never pings --
# the module's own docstring already states it isn't a failure.
_OAUTH_CHECK_KEYS = frozenset(key for key, _, _ in _OAUTH_CHECKS)

# A blip resolves in minutes to hours; something still unreachable/rate-
# limited/never-verified a full day later isn't self-healing, it's broken
# (or, for "unknown," neglected) -- the operator's own "threshold, not just
# data source" rule, applied here the same way email/calendar signals
# already use it. Reuses status_changed_ts (_record()'s own transition
# timestamp, already maintained for every key) rather than new bookkeeping.
_PERSISTENT_THRESHOLD_S = 24 * 3600


def scheduler_signal_action_needed(user_id: int) -> "tuple[str, str] | None":
    """auth_expired/scope_missing/api_disabled across every integration
    this module tracks, minus the one real overlap: an OAuth provider's
    own auth_expired here is the identical connected_accounts.needs_reconnect
    row connected_accounts.py's own account_needs_reconnect signal already
    pings about (authed_request() calls mark_needs_reconnect on the same
    401 this reads) -- including it here too would nag about the same dead
    account through two different signals. Everything else genuinely has
    no other path: scope_missing/api_disabled on an OAuth provider (the
    account is still "connected" by connected_accounts' own bookkeeping;
    only a live probe reveals the scope/API problem, which connected_accounts
    never learns about), and any actionable state at all for Home Assistant
    or an MCP server, neither of which has a connected_accounts row to be
    covered by at all.

    Zero extra cost: reads only get_all()'s cached rows, the same ones the
    settings page and check_integration_health already read -- no fresh
    probe. Per-key try/except so one malformed entry can't take out
    another's check or the rest of the ping cycle."""
    try:
        entries = get_all()
    except Exception as exc:  # noqa: BLE001 -- a signal check must never itself crash the ping cycle
        print(f"integration_health.scheduler_signal_action_needed: user {user_id}: {exc}", flush=True)
        return None
    broken = []
    for key, info in entries.items():
        try:
            status = info.get("status")
            if status not in ("auth_expired", "scope_missing", "api_disabled"):
                continue
            if status == "auth_expired" and key in _OAUTH_CHECK_KEYS:
                continue  # connected_accounts.account_needs_reconnect's own signal covers this
            broken.append((key, info))
        except Exception as exc:  # noqa: BLE001
            print(f"integration_health.scheduler_signal_action_needed: user {user_id} ({key}): {exc}", flush=True)
    if not broken:
        return None

    broken.sort(key=lambda pair: pair[0])
    parts = [f"{info['label']} ({info.get('explain') or info.get('detail') or info['status']})"
            for _, info in broken]
    dedup_token = ",".join(k for k, _ in broken)
    message = ("an integration needs attention: " + "; ".join(parts) +
              " -- see Settings > Integration health.")
    return message, dedup_token


def scheduler_signal_degraded(user_id: int) -> "tuple[str, str] | None":
    """The residue: error (a genuinely unclassified real failure -- included
    immediately, no threshold, since it's already a real classified event,
    not a maybe-transient one) and unreachable/rate_limited/unknown, but
    ONLY once persisted past _PERSISTENT_THRESHOLD_S -- a fresh unknown
    (a brand-new MCP connection, Tavily before its first manual check) or
    a momentary blip is exactly the noise a low-frequency catch-all must
    not become. One signal, one toggle for the whole bucket, since none of
    this is individually worth its own nag the way a dead OAuth connection
    or a wrong API key is."""
    now = time.time()
    try:
        entries = get_all()
    except Exception as exc:  # noqa: BLE001
        print(f"integration_health.scheduler_signal_degraded: user {user_id}: {exc}", flush=True)
        return None
    degraded = []
    for key, info in entries.items():
        try:
            status = info.get("status")
            if status == "error":
                degraded.append((key, info))
            elif status in ("unreachable", "rate_limited", "unknown"):
                changed = info.get("changed_ts")
                if changed and (now - changed) >= _PERSISTENT_THRESHOLD_S:
                    degraded.append((key, info))
        except Exception as exc:  # noqa: BLE001
            print(f"integration_health.scheduler_signal_degraded: user {user_id} ({key}): {exc}", flush=True)
    if not degraded:
        return None

    degraded.sort(key=lambda pair: pair[0])
    parts = [f"{info['label']} ({info['status']})" for _, info in degraded]
    dedup_token = ",".join(k for k, _ in degraded)
    message = ("something's degraded: " + "; ".join(parts) +
              " -- see Settings > Integration health for detail.")
    return message, dedup_token


def tick() -> None:
    """Reused background cadence (scheduler.py's own per-cycle call),
    same as PACI's own peers.tick() -- no new timer. Gated on
    home_assistant's own last_checked_ts as a sentinel (arbitrary but
    consistent choice) so the whole sweep moves together on one
    interval rather than each key drifting onto its own clock."""
    admin = _primary_admin()
    if admin is None:
        return
    interval_min = config.get("workspace", admin["workspace_id"], "integration_health_interval_minutes")
    interval_s = max(int(interval_min or 5), 1) * 60
    sentinel = health_status("home_assistant")
    if sentinel["checked_ts"] and (time.time() - sentinel["checked_ts"]) < interval_s:
        return
    run_all_checks(live_tavily=False)


def tool_view() -> dict[str, dict]:
    """The trimmed shape Nori's own tool actually returns -- label,
    status, and explain only. Deliberately drops raw detail/timestamps
    (get_all()'s own full shape, for the settings page and an admin's
    own debugging) -- this rides in a tool response mid-turn, not her
    standing context, so it stays short: thirteen-odd short sentences,
    not thirteen technical error dumps."""
    return {key: {"label": v["label"], "status": v["status"], "explain": v["explain"]}
           for key, v in get_all().items()}


def _check_integration_health_impl(session: dict) -> dict:
    return {"integrations": tool_view()}


def _register_tools() -> None:
    import tools  # local: same load-order reasoning every other domain module's late import uses

    tools.register(tools.Tool(
        "check_integration_health",
        {"type": "function", "function": {
            "name": "check_integration_health",
            "description": ("Check the real, current status of every outward-facing integration "
                            "you have -- Home Assistant, web search, and every connected Google/"
                            "Microsoft account and MCP server. Call this BEFORE telling anyone a "
                            "capability isn't working, a message didn't arrive, or a tool call "
                            "failed for an unclear reason -- confirm what's actually going on "
                            "rather than guessing or inventing a reason. Each entry's own 'explain' "
                            "field is written for you to relay directly, in plain terms, naming "
                            "what's actually wrong and what would fix it -- 'not configured' is not "
                            "a failure (say so plainly: a key or connection nobody's set up yet is "
                            "different from one that's broken), 'auth_expired' means it needs to be "
                            "reconnected, and so on. Prefer relaying 'explain' verbatim or close to "
                            "it over re-describing the status yourself. Cached from a recent "
                            "automatic check (at most a few minutes old), not a fresh probe made "
                            "just now -- close enough to trust for \"is this working right now.\""),
            "parameters": {"type": "object", "properties": {}}}},
        _check_integration_health_impl, min_role="member", data_scope="workspace", risk_tier="A"))


_register_tools()

import scheduler  # local-at-module-bottom: registers this module's signals once, at import
scheduler.register_signal(scheduler_signal_action_needed, key="integration_needs_attention",
                          label="An integration needs reconnecting or reconfiguring",
                          tier="routine", cooldown_seconds=24 * 3600)
scheduler.register_signal(scheduler_signal_degraded, key="integration_degraded",
                          label="An integration seems degraded (low-frequency)",
                          tier="routine", cooldown_seconds=48 * 3600)

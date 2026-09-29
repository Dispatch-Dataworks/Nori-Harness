# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Email and calendar tool shapes -- this is the real boundary line:
schema-complete, RBAC-correct, and genuinely wired to
each provider's real API endpoints, but nothing here can actually be
exercised without a real connected account, which needs the operator's
own OAuth credentials (see oauth.py / connected_accounts.py / Phase 10).
Every function below goes through connected_accounts.authed_request(),
which is what actually reports "not connected" / "needs reconnecting" /
whatever the provider's real error was -- nothing here checks connection
status by hand anymore (2026-09-18, see that function's own docstring).

triage_email is where the reader/actor split (ingest.py, Phase 6) meets a
real untrusted-content source for the first time: the raw email body
never reaches a tool-capable context directly -- it goes through
ingest.summarize_untrusted() first, and only the structured result comes
back to the model. list_emails deliberately returns bare IDs only, for
BOTH providers -- Outlook's used to also return the raw subject line
unscreened (2026-09-18, real gap found: Gmail's list never did this,
Outlook's always did, an inconsistency rather than a deliberate choice).
A subject is real, untrusted email content same as a body; if you want
to know what an email's about, triage_email is the one path built to
look at content safely.
"""
from __future__ import annotations

import base64
import datetime
import time
import urllib.parse
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import config
import store

import connected_accounts
import ingest

_GMAIL_LIST_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
_GMAIL_GET_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}"
_GMAIL_DRAFTS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
_GMAIL_LABELS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/labels"
_GMAIL_MODIFY_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}/modify"
_GCAL_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_OUTLOOK_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
_OUTLOOK_EVENTS_URL = "https://graph.microsoft.com/v1.0/me/events"
_OUTLOOK_CALENDARVIEW_URL = "https://graph.microsoft.com/v1.0/me/calendarview"
_OUTLOOK_CATEGORIES_URL = "https://graph.microsoft.com/v1.0/me/outlook/masterCategories"


def _list_emails_impl(session: dict, provider: str = "gmail", limit: int = 10) -> dict:
    if provider not in ("gmail", "outlook"):
        return {"error": "provider must be gmail or outlook"}
    url = (f"{_GMAIL_LIST_URL}?maxResults={min(int(limit), 25)}" if provider == "gmail"
          else f"{_OUTLOOK_MESSAGES_URL}?$top={min(int(limit), 25)}")
    result = connected_accounts.authed_request(session["user_id"], provider, url)
    if not result.get("ok"):
        return {"error": result["error"]}
    data = result["data"]
    key = "messages" if provider == "gmail" else "value"
    return {"messages": [{"id": m["id"]} for m in data.get(key, [])]}


def _fetch_email_body(user_id: int, email_id: str, provider: str) -> dict:
    """{"ok": True, "body": str} or {"ok": False, "error": str} -- shared
    by _triage_email_impl (the model-facing tool) and the proactive-ping
    email signal below, so there's exactly one place that knows how to
    turn an id into a raw body per provider."""
    url = (f"{_GMAIL_GET_URL.format(id=email_id)}?format=full" if provider == "gmail"
          else f"{_OUTLOOK_MESSAGES_URL}/{email_id}")
    result = connected_accounts.authed_request(user_id, provider, url)
    if not result.get("ok"):
        return {"ok": False, "error": result["error"]}
    data = result["data"]
    body = data.get("snippet", "") if provider == "gmail" else (data.get("body") or {}).get("content", "")
    return {"ok": True, "body": body}


def _triage_email_impl(session: dict, email_id: str, provider: str = "gmail") -> dict:
    """The real reader/actor split, applied to a real untrusted source for
    the first time: fetches the raw email, then hands it to
    ingest.summarize_untrusted() -- the model never sees the raw body
    directly in a tool-capable context, only the structured result below."""
    if provider not in ("gmail", "outlook"):
        return {"error": "provider must be gmail or outlook"}
    fetched = _fetch_email_body(session["user_id"], email_id, provider)
    if not fetched["ok"]:
        return {"error": fetched["error"]}
    return ingest.summarize_untrusted(fetched["body"], kind="email")


_GCAL_MAX_RESULTS = 20


def _fmt_gcal_time(t: dict, calendar_tz: str) -> tuple[str, bool]:
    """(formatted string, is_all_day). An all-day event carries `date`
    (no time component at all -- Google's own signal, not inferred);
    everything else carries `dateTime` with a real UTC offset already
    baked in, converted here to the CALENDAR's own reported timezone
    (2026-09-18, real bug: this used to be dropped entirely, and before
    that would have meant guessing his local zone -- Google already
    tells us what zone the calendar itself is in, on the same response
    this reads from, so there's nothing to assume)."""
    if "date" in t:
        return t["date"], True
    raw = t.get("dateTime", "")
    try:
        dt = datetime.datetime.fromisoformat(raw).astimezone(ZoneInfo(calendar_tz))
        return dt.strftime("%Y-%m-%d %H:%M %Z"), False
    except (ValueError, KeyError):
        return raw, False


def _gcal_event_row(e: dict, calendar_tz: str) -> dict:
    start, all_day = _fmt_gcal_time(e.get("start") or {}, calendar_tz)
    end, _ = _fmt_gcal_time(e.get("end") or {}, calendar_tz)
    row = {"id": e.get("id"), "summary": e.get("summary") or "(no title)",
          "start": start, "end": end, "all_day": all_day}
    if e.get("location"):
        row["location"] = e["location"]
    attendees = [a.get("displayName") or a.get("email") for a in (e.get("attendees") or [])
                if a.get("displayName") or a.get("email")]
    if attendees:
        row["attendees"] = attendees[:8]
    return row


def _outlook_event_row(e: dict) -> dict:
    start_raw, end_raw = (e.get("start") or {}), (e.get("end") or {})
    return {"id": e.get("id"), "summary": e.get("subject") or "(no title)",
           "start": f"{start_raw.get('dateTime','')} {start_raw.get('timeZone','')}".strip(),
           "end": f"{end_raw.get('dateTime','')} {end_raw.get('timeZone','')}".strip(),
           "all_day": bool(e.get("isAllDay")),
           **({"location": e["location"]["displayName"]}
              if (e.get("location") or {}).get("displayName") else {})}


def _list_calendar_events_impl(session: dict, provider: str = "google_calendar") -> dict:
    if provider not in ("google_calendar", "outlook"):
        return {"error": "provider must be google_calendar or outlook"}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if provider == "google_calendar":
        # timeMin/singleEvents/orderBy/maxResults (2026-09-18, real bug:
        # none of these were set, so this returned up to 250 events with
        # no time bound and no order at all -- on his real calendar that
        # meant a decade-old, effectively-random slice, not "upcoming."
        # singleEvents=true expands a recurring series into real
        # occurrences instead of one master row carrying an RRULE.
        params = {"timeMin": now, "singleEvents": "true", "orderBy": "startTime",
                 "maxResults": _GCAL_MAX_RESULTS}
        url = f"{_GCAL_EVENTS_URL}?{urllib.parse.urlencode(params)}"
    else:
        # /calendarview, not /events (2026-09-18 -- the real fix the
        # previous pass left open): /events with $orderby/$top matched
        # Google's ordering/cap but not its actual behavior -- it doesn't
        # time-bound the query at all, and returns each recurring series
        # as one master row (an RRULE), not real upcoming occurrences.
        # /calendarview requires an explicit start/end window (Graph's own
        # requirement, not an arbitrary choice here) and expands recurrence
        # into real instances within it automatically -- the direct Outlook
        # equivalent of Google's timeMin+singleEvents combination. Window:
        # now -> now+30d, same "upcoming" intent as Google's open-ended
        # timeMin, bounded on the far end since calendarview requires an
        # end. Still never verified live -- no Outlook account has ever
        # been connected here.
        end = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(days=30)).isoformat()
        params = {"startDateTime": now, "endDateTime": end,
                 "$orderby": "start/dateTime", "$top": _GCAL_MAX_RESULTS}
        url = f"{_OUTLOOK_CALENDARVIEW_URL}?{urllib.parse.urlencode(params)}"
    result = connected_accounts.authed_request(session["user_id"], provider, url)
    if not result.get("ok"):
        return {"error": result["error"]}
    data = result["data"]
    items = data.get("items", data.get("value", []))
    # summary/subject/location/attendees ride in unscreened -- NOT fixed
    # here, same flagged (not silently included) gap as before this fix.
    # A shared invite is real untrusted content, same class of risk an
    # email body is; this pass was scoped to making the output correct
    # (time/order/shape), not to screening.
    if provider == "google_calendar":
        calendar_tz = data.get("timeZone") or "UTC"
        return {"events": [_gcal_event_row(e, calendar_tz) for e in items]}
    return {"events": [_outlook_event_row(e) for e in items]}


def _create_calendar_event_impl(session: dict, provider: str, title: str, start: str, end: str) -> dict:
    # Outlook's scope was Calendars.Read-only until 2026-09-18 (oauth.py)
    # -- this POST would have 403'd every time until that widened to
    # Calendars.ReadWrite. Left this comment so the connection between "why
    # does this even work" and the scope entry isn't lost the next time
    # someone reads just this function.
    if provider not in ("google_calendar", "outlook"):
        return {"error": "provider must be google_calendar or outlook"}
    if provider == "google_calendar":
        url, body = _GCAL_EVENTS_URL, {"summary": title, "start": {"dateTime": start}, "end": {"dateTime": end}}
    else:
        url = _OUTLOOK_EVENTS_URL
        body = {"subject": title, "start": {"dateTime": start, "timeZone": "UTC"},
               "end": {"dateTime": end, "timeZone": "UTC"}}
    result = connected_accounts.authed_request(session["user_id"], provider, url, method="POST", body=body)
    if not result.get("ok"):
        return {"error": result["error"]}
    return {"ok": True, "event_id": result["data"].get("id")}


# ── drafts (Gmail + Outlook, 2026-09-18) -----------------------------------
def _create_draft_impl(session: dict, to: str, subject: str, body: str, provider: str = "gmail") -> dict:
    """Creates a real draft, Gmail or Outlook. NEVER sends -- for Gmail
    that's a code guarantee, not a scope one (checked directly: drafts.
    create and drafts.send/messages.send all accept the exact same
    scopes, gmail.modify included -- no Gmail scope permits one but not
    the other). For Outlook it's BOTH: Mail.ReadWrite (the granted scope)
    doesn't include Mail.Send at all, a real Graph-enforced separation,
    on top of this file having no function that calls either send
    endpoint. Either way: don't add a send tool without a real, separate
    decision to allow it. A created draft sits exactly like one written
    by hand -- visible, editable, NOT sent until a human opens it."""
    if provider not in ("gmail", "outlook"):
        return {"error": "provider must be gmail or outlook"}
    if provider == "gmail":
        msg = MIMEText(body)
        msg["To"] = to
        msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        result = connected_accounts.authed_request(
            session["user_id"], "gmail", _GMAIL_DRAFTS_URL, method="POST", body={"message": {"raw": raw}})
        draft_id = result["data"].get("id") if result.get("ok") else None
    else:
        # POST to /me/messages creates the message and leaves it in Drafts
        # -- it's only ever sent by a separate, explicit /send call this
        # file never makes.
        gbody = {"subject": subject, "body": {"contentType": "Text", "content": body},
                 "toRecipients": [{"emailAddress": {"address": to}}]}
        result = connected_accounts.authed_request(
            session["user_id"], "outlook", _OUTLOOK_MESSAGES_URL, method="POST", body=gbody)
        draft_id = result["data"].get("id") if result.get("ok") else None
    if not result.get("ok"):
        return {"error": result["error"]}
    where = "Gmail" if provider == "gmail" else "Outlook"
    return {"ok": True, "draft_id": draft_id,
           "note": f"saved as a draft in {where}, not sent -- he'll see it there before anything happens."}


def _list_email_labels_impl(session: dict, provider: str = "gmail") -> dict:
    """The real label/category list -- modify_email_labels needs a real
    name to resolve, not a guessed one. Gmail: real labels (folders),
    opaque ids, several system ones (INBOX, IMPORTANT...) don't match
    their own display name. Outlook: master categories, the closest real
    analog -- Outlook has no Gmail-style multi-label system on messages;
    categories are the one thing a message can carry more than one of.
    Moving a message between actual mail FOLDERS is a different,
    single-slot concept this doesn't touch -- not built, flagged rather
    than silently absent."""
    if provider not in ("gmail", "outlook"):
        return {"error": "provider must be gmail or outlook"}
    if provider == "gmail":
        result = connected_accounts.authed_request(session["user_id"], "gmail", _GMAIL_LABELS_URL)
        if not result.get("ok"):
            return {"error": result["error"]}
        return {"labels": [{"id": lbl["id"], "name": lbl["name"]} for lbl in result["data"].get("labels", [])]}
    result = connected_accounts.authed_request(session["user_id"], "outlook", _OUTLOOK_CATEGORIES_URL)
    if not result.get("ok"):
        return {"error": result["error"]}
    # Outlook categories have no separate id -- the display name IS the
    # identifier a message's own `categories` array stores, so id and name
    # are the same string here (unlike Gmail's opaque label ids).
    return {"labels": [{"id": cat["displayName"], "name": cat["displayName"]}
                       for cat in result["data"].get("value", [])]}


def _resolve_label_ids(session: dict, names: list, provider: str) -> dict:
    listed = _list_email_labels_impl(session, provider=provider)
    if "error" in listed:
        return listed
    by_name = {lbl["name"].lower(): lbl["id"] for lbl in listed["labels"]}
    ids, missing = [], []
    for name in names:
        lid = by_name.get(name.lower())
        if lid:
            ids.append(lid)
        else:
            missing.append(name)
    if missing:
        available = ", ".join(lbl["name"] for lbl in listed["labels"])
        return {"error": f"no such label(s): {', '.join(missing)} -- available labels: {available}"}
    return {"ids": ids}


def _modify_email_labels_impl(session: dict, email_id: str, add_labels: list | None = None,
                              remove_labels: list | None = None, provider: str = "gmail") -> dict:
    """Apply a label, remove one, or move a message (e.g. out of the
    inbox) by doing both at once. Gmail: a folder IS a label, one call
    covers add+remove+move together via addLabelIds/removeLabelIds.
    Outlook: categories don't have an add/remove endpoint -- PATCHing
    `categories` REPLACES the whole array, so this reads the message's
    current categories first, computes the new set (existing - remove +
    add) itself, then PATCHes that full set -- never a blind overwrite of
    categories this call didn't ask about. Label/category names are
    resolved against the real list either way -- an unknown name fails
    with the real available names, never a silent no-op."""
    if provider not in ("gmail", "outlook"):
        return {"error": "provider must be gmail or outlook"}
    add_labels, remove_labels = add_labels or [], remove_labels or []
    if not add_labels and not remove_labels:
        return {"error": "add_labels or remove_labels is required"}
    add_ids = _resolve_label_ids(session, add_labels, provider) if add_labels else {"ids": []}
    if "error" in add_ids:
        return add_ids
    remove_ids = _resolve_label_ids(session, remove_labels, provider) if remove_labels else {"ids": []}
    if "error" in remove_ids:
        return remove_ids
    if provider == "gmail":
        result = connected_accounts.authed_request(
            session["user_id"], "gmail", _GMAIL_MODIFY_URL.format(id=email_id), method="POST",
            body={"addLabelIds": add_ids["ids"], "removeLabelIds": remove_ids["ids"]})
    else:
        current = connected_accounts.authed_request(
            session["user_id"], "outlook", f"{_OUTLOOK_MESSAGES_URL}/{email_id}?$select=categories")
        if not current.get("ok"):
            return {"error": current["error"]}
        existing = set(current["data"].get("categories") or [])
        new_categories = sorted((existing - set(remove_ids["ids"])) | set(add_ids["ids"]))
        result = connected_accounts.authed_request(
            session["user_id"], "outlook", f"{_OUTLOOK_MESSAGES_URL}/{email_id}", method="PATCH",
            body={"categories": new_categories})
    if not result.get("ok"):
        return {"error": result["error"]}
    return {"ok": True, "applied": add_labels, "removed": remove_labels}


# ── tool registration ────────────────────────────────────────────────────
def _register_tools() -> None:
    import tools  # local: same reasoning as every other subsystem here

    tools.register(tools.Tool(
        "list_emails",
        {"type": "function", "function": {
            "name": "list_emails",
            "description": "List recent email message IDs from a connected account.",
            "parameters": {"type": "object", "properties": {
                "provider": {"type": "string", "enum": ["gmail", "outlook"]},
                "limit": {"type": "integer", "default": 10}}}}},
        # peer_blocked (2026-09-18, proposed rather than assumed): email is
        # his private correspondence, held back from any peer-motivated
        # turn entirely until he says otherwise. See connected_accounts.py.
        _list_emails_impl, min_role="member", data_scope="self", risk_tier="A",
        owner_check=connected_accounts.peer_blocked))

    tools.register(tools.Tool(
        "triage_email",
        {"type": "function", "function": {
            "name": "triage_email",
            "description": ("Read one email safely -- runs it through the untrusted-content "
                            "summarizer rather than exposing the raw body directly."),
            "parameters": {"type": "object", "properties": {
                "email_id": {"type": "string"},
                "provider": {"type": "string", "enum": ["gmail", "outlook"]}},
                "required": ["email_id"]}}},
        _triage_email_impl, min_role="member", data_scope="self", risk_tier="A",
        owner_check=connected_accounts.peer_blocked))

    tools.register(tools.Tool(
        "list_calendar_events",
        {"type": "function", "function": {
            "name": "list_calendar_events",
            "description": ("List upcoming events from a connected calendar -- title, start/end "
                            "in the calendar's own local time, whether it's all-day, and "
                            "location/attendees when present. Recurring events come back as "
                            "real upcoming occurrences, not one row per series."),
            "parameters": {"type": "object", "properties": {
                "provider": {"type": "string", "enum": ["google_calendar", "outlook"]}}}}},
        # peer_trust_gate (2026-09-18, proposed rather than assumed):
        # lower-stakes than email -- open to a peer-motivated turn once
        # that specific peer is fully trusted, same bar "what can they ask
        # you to change" already uses. See connected_accounts.py.
        _list_calendar_events_impl, min_role="member", data_scope="self", risk_tier="A",
        owner_check=connected_accounts.peer_trust_gate))

    tools.register(tools.Tool(
        "create_calendar_event",
        {"type": "function", "function": {
            "name": "create_calendar_event",
            "description": "Create a real event on a connected calendar.",
            "parameters": {"type": "object", "properties": {
                "provider": {"type": "string", "enum": ["google_calendar", "outlook"]},
                "title": {"type": "string"},
                "start": {"type": "string", "description": "ISO 8601 datetime"},
                "end": {"type": "string", "description": "ISO 8601 datetime"}},
                "required": ["provider", "title", "start", "end"]}}},
        # Conservative default, same reasoning as dispatch_subagent: a real
        # write to a real external system is a real-world effect, even
        # though it's self-scoped (only the calling user's own calendar).
        _create_calendar_event_impl, min_role="admin", data_scope="self", risk_tier="C",
        owner_check=connected_accounts.peer_trust_gate))

    tools.register(tools.Tool(
        "create_draft",
        {"type": "function", "function": {
            "name": "create_draft",
            "description": ("Create an email draft, Gmail or Outlook. This can NEVER send -- there "
                            "is no send tool for either provider. The draft sits there, visible and "
                            "editable, until he opens it himself and decides whether to send it."),
            "parameters": {"type": "object", "properties": {
                "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"},
                "provider": {"type": "string", "enum": ["gmail", "outlook"], "default": "gmail"}},
                "required": ["to", "subject", "body"]}}},
        # peer_blocked, same as list_emails/triage_email: drafting is
        # still an action on his private mailbox.
        _create_draft_impl, min_role="member", data_scope="self", risk_tier="B",
        owner_check=connected_accounts.peer_blocked))

    tools.register(tools.Tool(
        "list_email_labels",
        {"type": "function", "function": {
            "name": "list_email_labels",
            "description": ("List labels on the connected account -- Gmail's real labels "
                            "(folders), or Outlook's master categories (the closest analog; "
                            "Outlook has no multi-label system on messages)."),
            "parameters": {"type": "object", "properties": {
                "provider": {"type": "string", "enum": ["gmail", "outlook"], "default": "gmail"}}}}},
        _list_email_labels_impl, min_role="member", data_scope="self", risk_tier="A",
        owner_check=connected_accounts.peer_blocked))

    tools.register(tools.Tool(
        "modify_email_labels",
        {"type": "function", "function": {
            "name": "modify_email_labels",
            "description": ("Apply, remove, or move (both at once) labels/categories on one "
                            "message. Use list_email_labels first for real names -- an unknown "
                            "name fails rather than silently doing nothing."),
            "parameters": {"type": "object", "properties": {
                "email_id": {"type": "string"},
                "add_labels": {"type": "array", "items": {"type": "string"}},
                "remove_labels": {"type": "array", "items": {"type": "string"}},
                "provider": {"type": "string", "enum": ["gmail", "outlook"], "default": "gmail"}},
                "required": ["email_id"]}}},
        _modify_email_labels_impl, min_role="member", data_scope="self", risk_tier="B",
        owner_check=connected_accounts.peer_blocked))


_register_tools()


# ── proactive-ping signal: calendar (2026-09-26) ─────────────────────────
# Structured data straight from the provider -- no LLM needed, so this is
# cheap enough to check every ping cycle (min_check_interval_seconds=None).
_CALENDAR_LOOKAHEAD_MIN = 20


def _gcal_soonest_event(user_id: int, now: "datetime.datetime", lookahead: "datetime.datetime") -> "dict | None":
    params = {"timeMin": now.isoformat(), "timeMax": lookahead.isoformat(),
             "singleEvents": "true", "orderBy": "startTime", "maxResults": 5}
    result = connected_accounts.authed_request(user_id, "google_calendar",
                                               f"{_GCAL_EVENTS_URL}?{urllib.parse.urlencode(params)}")
    if not result.get("ok"):
        # Logged here, not just returned -- a background ping check has no UI observer to
        # show this to; without this print, an expired/misconfigured Google connection makes
        # the calendar signal go permanently, silently quiet (2026-09-26, same class as the
        # blank-OPENROUTER_API_KEY gap: a config failure that produced nothing server-side).
        print(f"email_calendar._gcal_soonest_event: user {user_id}: {result.get('error')}", flush=True)
        return None
    for e in result["data"].get("items", []):
        start = (e.get("start") or {}).get("dateTime")  # no dateTime = an all-day event, skip: no specific time to be "soon"
        if not start:
            continue
        try:
            start_dt = datetime.datetime.fromisoformat(start)
        except ValueError:
            continue
        return {"id": e.get("id"), "summary": e.get("summary") or "(no title)", "start_dt": start_dt}
    return None


def _outlook_soonest_event(user_id: int, now: "datetime.datetime", lookahead: "datetime.datetime") -> "dict | None":
    """Same shape as _gcal_soonest_event -- never verified live, same
    caveat _list_calendar_events_impl already carries (no Outlook account
    has ever been connected here)."""
    params = {"startDateTime": now.isoformat(), "endDateTime": lookahead.isoformat(),
             "$orderby": "start/dateTime", "$top": 5}
    result = connected_accounts.authed_request(user_id, "outlook",
                                               f"{_OUTLOOK_CALENDARVIEW_URL}?{urllib.parse.urlencode(params)}")
    if not result.get("ok"):
        print(f"email_calendar._outlook_soonest_event: user {user_id}: {result.get('error')}", flush=True)
        return None
    for e in result["data"].get("value", []):
        start = (e.get("start") or {}).get("dateTime")
        if not start:
            continue
        try:
            start_dt = datetime.datetime.fromisoformat(start).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        return {"id": e.get("id"), "summary": e.get("subject") or "(no title)", "start_dt": start_dt}
    return None


def calendar_scheduler_signal(user_id: int) -> "tuple[str, str] | None":
    """A calendar event starting soon -- the operator: "a calendar event in twenty
    minutes is worth mentioning; one next week isn't." Dedupes PER EVENT
    (the event's own id as dedup_token, registered below) rather than as
    a whole signal -- so mentioning today's 2pm doesn't cool down and
    silently swallow a genuinely different 4pm meeting later the same
    day. Checks Google first, then Outlook, skipping silently whichever
    isn't connected."""
    now = datetime.datetime.now(datetime.timezone.utc)
    lookahead = now + datetime.timedelta(minutes=_CALENDAR_LOOKAHEAD_MIN)
    for provider, fetch in (("google_calendar", _gcal_soonest_event), ("outlook", _outlook_soonest_event)):
        if connected_accounts.get(user_id, provider) is None:
            continue
        try:
            event = fetch(user_id, now, lookahead)
        except Exception as exc:  # noqa: BLE001 -- one provider's outage must not block the other, or the rest of the ping cycle
            print(f"email_calendar.calendar_scheduler_signal: user {user_id} ({provider}) raised: {exc}", flush=True)
            continue
        if event:
            minutes = max(0, int((event["start_dt"] - now).total_seconds() // 60))
            return f"a calendar event starting in about {minutes} min: \"{event['summary']}\"", event["id"]
    return None


import scheduler  # local-at-module-bottom on purpose: registers this module's signals once, at import
scheduler.register_signal(calendar_scheduler_signal, key="upcoming_calendar_event",
                          label="Upcoming calendar event (within 20 minutes)",
                          tier="urgent", cooldown_seconds=30 * 60)


# ── proactive-ping signal: email (2026-09-26) ────────────────────────────
# The real cost driver among all the signals -- each candidate triaged is
# a genuine LLM call (ingest.summarize_untrusted). The operator: "an unread email
# isn't worth a nag; one that looks like it needs a reply and has sat
# three days might be" -- and separately, the check itself is capped to
# ping_signal_email_checks_per_day, spread across his own ping window
# (not a fixed interval around the clock, so none of the four land while
# he's asleep), with a real daily $ cap that SKIPS rather than borrows,
# and every check/skip logged so it's observable, never a silent stop.
_EMAIL_SIGNAL_KEY = "needs_reply_email"
_NEEDS_REPLY_MIN_AGE_DAYS = 3
_EMAIL_CANDIDATES_PER_CHECK = 3


def _email_check_interval_seconds(user_id: int) -> float:
    """ping_signal_email_checks_per_day checks, spread across his actual
    ping window, not a plain 24h/N -- the operator: "spread those four across his
    waking hours... four evenly spaced would waste two of them while
    he's asleep." Falls back to the full 24h if his window is malformed
    (start==end) rather than dividing by zero."""
    start = config.get("user", user_id, "ping_window_start")
    end = config.get("user", user_id, "ping_window_end")
    span_hours = (end - start) % 24
    if span_hours <= 0:
        span_hours = 24
    checks = max(1, int(config.get("user", user_id, "ping_signal_email_checks_per_day")))
    return (span_hours * 3600) / checks


def _log_email_signal_spend(user_id: int, *, cost_usd: "float | None", skipped: bool, note: str) -> None:
    store.write(lambda c: c.execute(
        "INSERT INTO ping_signal_spend(user_id,signal_key,ts,cost_usd,skipped,note) VALUES (?,?,?,?,?,?)",
        (user_id, _EMAIL_SIGNAL_KEY, time.time(), cost_usd, 1 if skipped else 0, note)))


def email_signal_spend_status(user_id: int) -> dict:
    """Real spend for THIS signal, last 24h -- the operator: "he should be able to
    see what the email signal has cost and whether it's been skipping...
    a cap that silently disables a feature he enabled is the failure
    mode we keep fixing." Read by server.py's cost tab, same idea as
    imagegen's own medialog.spend_status()."""
    since = time.time() - 24 * 3600
    rows = store.read(lambda c: c.execute(
        "SELECT cost_usd, skipped FROM ping_signal_spend WHERE user_id=? AND signal_key=? AND ts>=?",
        (user_id, _EMAIL_SIGNAL_KEY, since)).fetchall())
    return {"spent_today_usd": sum(r["cost_usd"] for r in rows if r["cost_usd"]),
           "checked_today": sum(1 for r in rows if not r["skipped"]),
           "skipped_today": sum(1 for r in rows if r["skipped"])}


def _recent_unread(user_id: int, provider: str, *, limit: int) -> list:
    """[(email_id, age_in_days), ...], newest first, capped at `limit` --
    the bound on how many emails might get triaged per check, independent
    of inbox size. Gmail: one list call (q=is:unread) plus one metadata
    call per candidate for its real received time -- still bounded by
    `limit`, never the whole inbox. Outlook: never verified live, same
    caveat as the rest of this file's Outlook paths."""
    now = time.time()
    if provider == "gmail":
        params = {"q": "is:unread", "maxResults": limit}
        result = connected_accounts.authed_request(user_id, "gmail", f"{_GMAIL_LIST_URL}?{urllib.parse.urlencode(params)}")
        if not result.get("ok"):
            # Same reasoning as _gcal_soonest_event's own print -- a background ping check
            # has no UI observer, so a misconfigured/expired connection must reach the logs
            # or it goes silently, permanently quiet.
            print(f"email_calendar._recent_unread: user {user_id} (gmail): {result.get('error')}", flush=True)
            return []
        out = []
        for m in result["data"].get("messages", [])[:limit]:
            meta_url = f"{_GMAIL_GET_URL.format(id=m['id'])}?format=metadata&metadataHeaders=Date"
            meta = connected_accounts.authed_request(user_id, "gmail", meta_url)
            internal_ms = meta["data"].get("internalDate") if meta.get("ok") else None
            age_days = (now - int(internal_ms) / 1000) / 86400 if internal_ms else 0.0
            out.append((m["id"], age_days))
        return out
    params = {"$filter": "isRead eq false", "$orderby": "receivedDateTime desc", "$top": limit}
    result = connected_accounts.authed_request(user_id, "outlook", f"{_OUTLOOK_MESSAGES_URL}?{urllib.parse.urlencode(params)}")
    if not result.get("ok"):
        print(f"email_calendar._recent_unread: user {user_id} (outlook): {result.get('error')}", flush=True)
        return []
    out = []
    for m in result["data"].get("value", [])[:limit]:
        received = m.get("receivedDateTime")
        try:
            received_dt = datetime.datetime.fromisoformat(received.replace("Z", "+00:00")) if received else None
        except ValueError:
            received_dt = None
        age_days = (now - received_dt.timestamp()) / 86400 if received_dt else 0.0
        out.append((m["id"], age_days))
    return out


def email_scheduler_signal(user_id: int) -> "tuple[str, str] | None":
    for provider in ("gmail", "outlook"):
        if connected_accounts.get(user_id, provider) is None:
            continue
        cap = float(config.get("user", user_id, "ping_signal_email_daily_cap_usd"))
        est = float(config.get("user", user_id, "ping_signal_email_cost_estimate_usd"))
        spent = email_signal_spend_status(user_id)["spent_today_usd"]
        if spent + est > cap + 1e-9:
            note = f"daily email-nag budget reached (${spent:.2f}/${cap:.2f})"
            _log_email_signal_spend(user_id, cost_usd=None, skipped=True, note=note)
            # The skip is already a real, observable row on the cost page (the operator's own earlier
            # requirement) -- also printed here so it reaches docker compose logs too, for an
            # operator who only has the logs, not the UI, of someone else's instance.
            print(f"email_calendar.email_scheduler_signal: user {user_id}: {note}", flush=True)
            return None
        try:
            candidates = _recent_unread(user_id, provider, limit=_EMAIL_CANDIDATES_PER_CHECK)
        except Exception as exc:  # noqa: BLE001 -- one provider's outage must not block the ping cycle
            print(f"email_calendar.email_scheduler_signal: user {user_id} ({provider}) raised: {exc}", flush=True)
            continue
        for email_id, age_days in candidates:
            if age_days < _NEEDS_REPLY_MIN_AGE_DAYS:
                continue
            fetched = _fetch_email_body(user_id, email_id, provider)
            if not fetched["ok"]:
                print(f"email_calendar.email_scheduler_signal: user {user_id} ({provider}) "
                     f"couldn't fetch {email_id}: {fetched['error']}", flush=True)
                continue
            sink = lambda usage: _log_email_signal_spend(  # noqa: E731 -- short-lived, not worth naming
                user_id, cost_usd=usage.get("cost"), skipped=False, note=f"triaged {email_id}")
            triaged = ingest.summarize_untrusted(fetched["body"], kind="email", cost_sink=sink)
            if triaged.get("suspicious"):
                continue  # a screening failure or an injection attempt is never treated as a real "needs reply" signal
            if triaged.get("category") == "actionable":
                return f"an unread email from about {age_days:.0f} day(s) ago looks like it needs a reply", email_id
        return None
    return None


scheduler.register_signal(email_scheduler_signal, key=_EMAIL_SIGNAL_KEY,
                          label="Unread email that looks like it needs a reply",
                          tier="routine", cooldown_seconds=3 * 3600,
                          min_check_interval_seconds=_email_check_interval_seconds)

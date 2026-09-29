# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Reminders (2026-09-16) -- like tasks, but urgent: presented at a due
time today, then nagged about a few times until it's done, rather than
sitting quietly on the board until someone looks. This module owns the
`reminders`/`reminder_events` tables; scheduler.py owns the actual
due-check/nag firing loop (see that module's own `_check_due_reminders`/
`_fire_reminder`) -- kept out of here on purpose, same reasoning
schedules.py's own module boundary already documents (chat/turns/
conversation import chains this module must stay clear of).

**The nag loop, reported and settled before building:** `nag_interval_
min` (default 30) and `nag_max_count` (default 3), both configurable
per reminder. The first contact, at the due time itself, counts as nag
#1 of nag_max_count -- "presented... and nags a few times" is one
counter, not a presentation plus a separate nag budget. Stops on
whichever comes first: marked done, nag_max_count reached (silently,
for the rest of that day), or a hard end-of-day cutoff -- a reminder
still pending at midnight local is logged 'missed' (a distinct outcome
from 'done') and, if recurring, advances to its next occurrence using
the exact catch-up-safe "advance from now" policy schedules.py already
established -- never a backlog of missed days replayed.

**Scheduler reuse, not a second timing engine:** does NOT become a
schedules.py row -- a schedule fires once and moves on; a reminder fires,
then re-fires ON ITS OWN with state that has to persist between those
re-fires (nag count, done-or-not), which schedules' own instruction/
metadata was never shaped to carry. What IS reused: scheduler.py's
background cycle gets a third check alongside its own `_check_due_
schedules`; the identical `turns.run()`/`chat.run()` firing shape; the
identical quiet-hours separation (she can act and speak at any hour,
notify_quiet_start/end only ever gates the client-side push
notification, confirmed unchanged rather than assumed); and
recurrence.py, extended (not duplicated) with the 'weekly'/
'monthly_day'/'monthly_weekday' patterns schedules' own two never needed.

**One reminder object, a history of occurrences** -- the operator's own
framing. Closing (or missing) an occurrence never creates a new
reminder row; it logs a 'completed' or 'missed' event against the SAME
row and, for a recurring reminder, advances its own next_due_ts/resets
its own nag_count in place. "A history of occurrences" lives entirely
in reminder_events (nags, progress, completion, missed), the same
actor-tagged shape reused a sixth time now -- no separate occurrences
table, matching how every other feature in this build keeps ONE object
plus ONE event log, never a second table just for history.

**"Track" -> "record progress"** (the operator's own clarification and
naming call, handed back to this module): a progress entry holds all
three things he asked about -- a free-text `note`, an optional `status`
marker from a small fixed set (acknowledged/snoozed/in_progress, kept
closed rather than open text for the same reason priority/category
started as closed sets), and a timestamp, which is just the event row's
own `ts`. Logged as action='progress', living in the identical history
as nag attempts and the completion record. Deliberately does NOT alter
nag timing on its own (e.g. 'snoozed' doesn't push the next nag back) --
flagged as a scope call, not silently built in.

**Categories**: domain="reminder" on catalog.py, same mechanism tasks.py/
notes.py already use.

**Peers and history**: same as tasks -- provenance (created_by_type/
created_by_peer_name, actor/actor_peer_name via peers.actor_for())
follows the identical three-way vocabulary. Peers ARE wired in
(2026-09-16, his own follow-up answer, once notes/tasks/reminders/
trackers all existed) -- see register_peer_actions() below, standard
trust ladder, same as schedules.py/homeassistant.py/memory.py.
"""
from __future__ import annotations

import time

import catalog
import recurrence
import store
import usertime
# tasks is NOT imported at module level: reminders -> tasks -> scheduler -> reminders would be a
# real import cycle now that tasks.py registers its own ping signal at import time (2026-09-26).
# Every use of tasks.parse_when() below is a local import instead, same fix shape as every other
# late-import-to-avoid-a-cycle comment in this codebase.

CATEGORY_DOMAIN = "reminder"
STATUSES = ("active", "closed")
OCCURRENCE_STATUSES = ("pending", "done", "missed")
PROGRESS_STATUSES = ("acknowledged", "snoozed", "in_progress")
# Reminders never offer 'interval' -- a reminder due "every 30 minutes"
# was never a stated need -- so this is recurrence.TYPES minus that one,
# plus None meaning "once" (a specific date, not a repeating pattern).
RECUR_TYPES = ("weekly", "monthly_day", "monthly_weekday", "time")

DEFAULT_NAG_INTERVAL_MIN = 30
DEFAULT_NAG_MAX_COUNT = 3
MIN_NAG_INTERVAL_MIN = 5
MAX_NAG_COUNT = 10

_TRACKED_FIELDS = ("name", "body", "category", "due_hour", "due_minute", "recur_type", "once_date",
                  "recur_weekday", "recur_month_day", "recur_week_ordinal",
                  "nag_interval_min", "nag_max_count")

_UNSET = object()


def day_bounds(user_id: int, ts: float) -> tuple[float, float]:
    """(start-of-day, end-of-day) for the calendar day `ts` falls in, HIS
    zone -- the hard nag cutoff (module docstring) is "past the end of
    THIS day," computed off this. Was server-OS-local time before
    2026-09-18; now usertime, same real-bug fix as everywhere else."""
    return usertime.day_start(user_id, ts), usertime.day_end(user_id, ts)


def _validate(session: dict, *, name: str, category: str, due_hour: int, due_minute: int,
             recur_type: str | None, once_date, recur_weekday: int | None,
             recur_month_day: int | None, recur_week_ordinal: int | None,
             nag_interval_min: int, nag_max_count: int) -> str | None:
    if not (name or "").strip():
        return "name can't be empty"
    if not catalog.is_valid(session, CATEGORY_DOMAIN, category):
        return f"{category!r} isn't a current reminder category -- check category_list, or category_add it first"
    if due_hour is None or due_minute is None or not (0 <= due_hour <= 23) or not (0 <= due_minute <= 59):
        return "due_hour/due_minute must be a valid 24-hour time"
    if recur_type is not None:
        if recur_type not in RECUR_TYPES:
            return f"recur_type must be one of: {', '.join(RECUR_TYPES)}, or omitted for a one-off"
        err = recurrence.validate(recur_type, time_hour=due_hour, time_minute=due_minute,
                                  weekday=recur_weekday, month_day=recur_month_day,
                                  week_ordinal=recur_week_ordinal)
        if err:
            return err
    else:
        import tasks
        if tasks.parse_when(once_date, session["user_id"]) is None:
            return f"couldn't understand once_date {once_date!r} -- needed for a one-off reminder"
    if not (MIN_NAG_INTERVAL_MIN <= nag_interval_min):
        return f"nag_interval_min must be at least {MIN_NAG_INTERVAL_MIN}"
    if not (1 <= nag_max_count <= MAX_NAG_COUNT):
        return f"nag_max_count must be between 1 and {MAX_NAG_COUNT}"
    return None


def _first_due(user_id: int, *, due_hour: int, due_minute: int, recur_type: str | None, once_date,
              recur_weekday: int | None, recur_month_day: int | None,
              recur_week_ordinal: int | None) -> float:
    now = time.time()
    tz = usertime.zone_for(user_id)
    if recur_type is None:
        import tasks
        anchor = tasks.parse_when(once_date, user_id)
        d = usertime.local_dt(user_id, anchor).date()
        return recurrence.mk(tz, d.year, d.month, d.day, due_hour, due_minute)
    return recurrence.compute_next(recur_type, time_hour=due_hour, time_minute=due_minute,
                                   weekday=recur_weekday, month_day=recur_month_day,
                                   week_ordinal=recur_week_ordinal, after=now, tz=tz)


def add(session: dict, *, name: str, category: str, due_hour: int, due_minute: int,
       body: str | None = None, recur_type: str | None = None, once_date=None,
       recur_weekday: int | None = None, recur_month_day: int | None = None,
       recur_week_ordinal: int | None = None, nag_interval_min: int = DEFAULT_NAG_INTERVAL_MIN,
       nag_max_count: int = DEFAULT_NAG_MAX_COUNT, created_by_type: str = "user",
       created_by_peer_name: str | None = None) -> dict:
    err = _validate(session, name=name, category=category, due_hour=due_hour, due_minute=due_minute,
                    recur_type=recur_type, once_date=once_date, recur_weekday=recur_weekday,
                    recur_month_day=recur_month_day, recur_week_ordinal=recur_week_ordinal,
                    nag_interval_min=nag_interval_min, nag_max_count=nag_max_count)
    if err:
        return {"error": err}
    next_due = _first_due(session["user_id"], due_hour=due_hour, due_minute=due_minute,
                          recur_type=recur_type, once_date=once_date, recur_weekday=recur_weekday,
                          recur_month_day=recur_month_day, recur_week_ordinal=recur_week_ordinal)
    once_date_str = None
    if recur_type is None:
        import tasks
        d = usertime.local_dt(session["user_id"], tasks.parse_when(once_date, session["user_id"])).date()
        once_date_str = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
    now = time.time()
    def _w(c):
        return c.execute(
            "INSERT INTO reminders(user_id, name, body, category, due_hour, due_minute, recur_type, "
            "once_date, recur_weekday, recur_month_day, recur_week_ordinal, nag_interval_min, "
            "nag_max_count, status, created_by_type, created_by_peer_name, created_ts, next_due_ts, "
            "occurrence_status, nag_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?,'pending',0)",
            (session["user_id"], name.strip(), body.strip() if body else None, category, due_hour,
             due_minute, recur_type, once_date_str, recur_weekday, recur_month_day, recur_week_ordinal,
             nag_interval_min, nag_max_count, created_by_type, created_by_peer_name, now, next_due)
        ).lastrowid
    rid = store.write(_w)
    _log_event(rid, created_by_type, created_by_peer_name, "created")
    return {"ok": True, "reminder_id": rid, "next_due_ts": next_due}


def _get_owned(session: dict, reminder_id: int) -> dict | None:
    row = store.read(lambda c: c.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,)).fetchone())
    if row is None or row["user_id"] != session["user_id"]:
        return None
    return dict(row)


def update(session: dict, reminder_id: int, *, name=_UNSET, body=_UNSET, category=_UNSET,
          due_hour=_UNSET, due_minute=_UNSET, recur_type=_UNSET, once_date=_UNSET,
          recur_weekday=_UNSET, recur_month_day=_UNSET, recur_week_ordinal=_UNSET,
          nag_interval_min=_UNSET, nag_max_count=_UNSET, actor: str = "user",
          actor_peer_name: str | None = None) -> dict:
    row = _get_owned(session, reminder_id)
    if row is None:
        return {"error": "no such reminder"}
    merged = dict(row)
    for key, val in (("name", name), ("body", body), ("category", category), ("due_hour", due_hour),
                     ("due_minute", due_minute), ("recur_type", recur_type),
                     ("recur_weekday", recur_weekday), ("recur_month_day", recur_month_day),
                     ("recur_week_ordinal", recur_week_ordinal), ("nag_interval_min", nag_interval_min),
                     ("nag_max_count", nag_max_count)):
        if val is not _UNSET:
            merged[key] = val
    once_date_for_validate = merged.get("once_date")
    if once_date is not _UNSET:
        once_date_for_validate = once_date
    err = _validate(session, name=merged["name"], category=merged["category"], due_hour=merged["due_hour"],
                    due_minute=merged["due_minute"], recur_type=merged["recur_type"],
                    once_date=once_date_for_validate, recur_weekday=merged["recur_weekday"],
                    recur_month_day=merged["recur_month_day"], recur_week_ordinal=merged["recur_week_ordinal"],
                    nag_interval_min=merged["nag_interval_min"], nag_max_count=merged["nag_max_count"])
    if err:
        return {"error": err}
    # A field that changes what "due" even means recomputes the CURRENT
    # occurrence's own next_due_ts from now, same as schedules.py's own
    # edit() does when its own timing shape changes -- the old due time
    # no longer means anything once the pattern itself changed.
    retime = any(v is not _UNSET for v in (due_hour, due_minute, recur_type, once_date, recur_weekday,
                                           recur_month_day, recur_week_ordinal))
    once_date_str = merged.get("once_date")
    if merged["recur_type"] is None:
        import tasks
        anchor = tasks.parse_when(once_date_for_validate, session["user_id"])
        d = usertime.local_dt(session["user_id"], anchor).date()
        once_date_str = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
    next_due = row["next_due_ts"]
    occurrence_status, nag_count, last_nag_ts = row["occurrence_status"], row["nag_count"], row["last_nag_ts"]
    if retime:
        next_due = _first_due(session["user_id"], due_hour=merged["due_hour"], due_minute=merged["due_minute"],
                              recur_type=merged["recur_type"], once_date=once_date_for_validate,
                              recur_weekday=merged["recur_weekday"], recur_month_day=merged["recur_month_day"],
                              recur_week_ordinal=merged["recur_week_ordinal"])
        occurrence_status, nag_count, last_nag_ts = "pending", 0, None
    final = {**merged, "name": merged["name"].strip(),
            "body": (merged["body"].strip() if merged.get("body") else None), "once_date": once_date_str}
    store.write(lambda c: c.execute(
        "UPDATE reminders SET name=?, body=?, category=?, due_hour=?, due_minute=?, recur_type=?, "
        "once_date=?, recur_weekday=?, recur_month_day=?, recur_week_ordinal=?, nag_interval_min=?, "
        "nag_max_count=?, next_due_ts=?, occurrence_status=?, nag_count=?, last_nag_ts=? WHERE id=?",
        (final["name"], final["body"], final["category"], final["due_hour"], final["due_minute"],
         final["recur_type"], final["once_date"], final["recur_weekday"], final["recur_month_day"],
         final["recur_week_ordinal"], final["nag_interval_min"], final["nag_max_count"], next_due,
         occurrence_status, nag_count, last_nag_ts, reminder_id)))
    changes = {}
    for f in _TRACKED_FIELDS:
        if row.get(f) != final.get(f):
            changes[f] = {"old": row.get(f), "new": final.get(f)}
    if changes:
        _log_event(reminder_id, actor, actor_peer_name, "updated", changes=changes)
    return {"ok": True}


def close(session: dict, reminder_id: int, *, note: str | None = None, actor: str = "user",
         actor_peer_name: str | None = None) -> dict:
    """Marks the CURRENT occurrence done -- never deletes, same rule
    every other object in this build follows. A recurring reminder
    advances to its next occurrence and stays status='active' (closing
    one occurrence completes it, not the reminder); a one-off reminder's
    own status becomes 'closed' for good. Logged as action='completed',
    living in the same history as nags and progress entries."""
    row = _get_owned(session, reminder_id)
    if row is None:
        return {"error": "no such reminder"}
    if row["status"] == "closed":
        return {"error": "already closed"}
    now = time.time()
    if row["recur_type"]:
        next_due = recurrence.compute_next(row["recur_type"], time_hour=row["due_hour"],
                                           time_minute=row["due_minute"], weekday=row["recur_weekday"],
                                           month_day=row["recur_month_day"],
                                           week_ordinal=row["recur_week_ordinal"], after=now,
                                           tz=usertime.zone_for(session["user_id"]))
        store.write(lambda c: c.execute(
            "UPDATE reminders SET occurrence_status='pending', nag_count=0, last_nag_ts=NULL, "
            "next_due_ts=? WHERE id=?", (next_due, reminder_id)))
        _log_event(reminder_id, actor, actor_peer_name, "completed", note=note)
        return {"ok": True, "recurred": True, "next_due_ts": next_due}
    store.write(lambda c: c.execute(
        "UPDATE reminders SET status='closed', occurrence_status='done' WHERE id=?", (reminder_id,)))
    _log_event(reminder_id, actor, actor_peer_name, "completed", note=note)
    return {"ok": True, "recurred": False}


def record_progress(session: dict, reminder_id: int, *, note: str | None = None, status: str | None = None,
                    actor: str = "user", actor_peer_name: str | None = None) -> dict:
    """Distinct from close() (which ends the occurrence) and from a nag
    (which is HER acting, not a record of what she observed) -- this is
    for logging where things stand mid-cycle: what he said, or a small
    fixed status marker (module docstring: acknowledged/snoozed/
    in_progress). Requires at least one of note/status -- a call with
    neither has nothing to record."""
    row = _get_owned(session, reminder_id)
    if row is None:
        return {"error": "no such reminder"}
    if status is not None and status not in PROGRESS_STATUSES:
        return {"error": f"status must be one of: {', '.join(PROGRESS_STATUSES)}"}
    if not note and not status:
        return {"error": "pass at least a note or a status -- nothing to record otherwise"}
    changes = {"status": {"old": None, "new": status}} if status else None
    _log_event(reminder_id, actor, actor_peer_name, "progress", changes=changes, note=note)
    return {"ok": True}


def set_needs_attention(session: dict, reminder_id: int, needs_attention: bool, *, actor: str = "user",
                        actor_peer_name: str | None = None) -> dict:
    """A new reminder starts needing his attention (schema default 1);
    viewing its full-screen board detail page is what actually clears
    it, not merely being rendered as a card (2026-09-17, his own design
    point: "everything marks read on page load" would make the feature
    do nothing). Nori (or a trusted peer) can set it either direction
    herself via this same function. Independent of close()/record_
    progress() -- neither touches it -- but NOT independent of the nag
    loop: mark_nagged() below re-raises it on every fresh nag, since a
    reminder actively nagging him again is exactly "needs attention"
    regardless of whether he'd viewed (and cleared) an earlier nag for
    the same occurrence."""
    row = _get_owned(session, reminder_id)
    if row is None:
        return {"error": "no such reminder"}
    new_val = 1 if needs_attention else 0
    if row["needs_attention"] == new_val:
        return {"ok": True}
    store.write(lambda c: c.execute("UPDATE reminders SET needs_attention=? WHERE id=?", (new_val, reminder_id)))
    _log_event(reminder_id, actor, actor_peer_name, "flagged",
              changes={"needs_attention": {"old": row["needs_attention"], "new": new_val}})
    return {"ok": True}


def list_for_user(session: dict, *, status: str | None = "active", category: str | None = None) -> dict:
    if status is not None and status not in STATUSES:
        return {"error": f"status must be one of: {', '.join(STATUSES)}, or omitted for all"}
    where = ["user_id=?"]
    params = [session["user_id"]]
    if status is not None:
        where.append("status=?")
        params.append(status)
    if category is not None:
        where.append("category=?")
        params.append(category)
    rows = store.read(lambda c: c.execute(
        f"SELECT * FROM reminders WHERE {' AND '.join(where)} ORDER BY next_due_ts", tuple(params)).fetchall())
    return {"reminders": [dict(r) for r in rows]}


def get_for_user(session: dict, reminder_id: int) -> dict | None:
    return _get_owned(session, reminder_id)


def _log_event(reminder_id: int, actor: str, actor_peer_name: str | None, action: str,
              changes: dict | None = None, note: str | None = None) -> None:
    import json
    store.write(lambda c: c.execute(
        "INSERT INTO reminder_events(reminder_id, ts, actor, actor_peer_name, action, changes, note) "
        "VALUES (?,?,?,?,?,?,?)",
        (reminder_id, time.time(), actor, actor_peer_name, action,
         json.dumps(changes) if changes else None, note)))


def history_for(reminder_id: int, limit: int = 100) -> list[dict]:
    import json
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM reminder_events WHERE reminder_id=? ORDER BY ts DESC LIMIT ?",
        (reminder_id, limit)).fetchall())
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["changes"] = json.loads(d["changes"]) if d.get("changes") else {}
        except (ValueError, TypeError):
            d["changes"] = {}
        out.append(d)
    return out


# ── used by scheduler.py's own due-check (same reasoning schedules.py's
# own due_schedules() documents -- deliberately NOT wired through
# scheduler.register_signal, which is gated by ping_enabled/ping_window/
# recently_active; reminders fire regardless of all three) ─────────────
def due_for_user(user_id: int, now: float | None = None) -> list[dict]:
    now = now if now is not None else time.time()
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM reminders WHERE user_id=? AND status='active' AND occurrence_status='pending' "
        "AND next_due_ts<=?", (user_id, now)).fetchall())
    return [dict(r) for r in rows]


def mark_nagged(reminder_id: int, fired_ts: float | None = None) -> None:
    """Also re-raises needs_attention (2026-09-17) -- a fresh nag IS a
    fresh "this needs attention" signal, even if he'd already viewed
    (and cleared) an earlier one for the same reminder. Logged only
    when it actually flips 0->1, same "Nori marked this needs-
    attention again" visibility he asked for, attributed to her since
    this fires from the scheduler's own due-check, never a live session."""
    fired_ts = fired_ts if fired_ts is not None else time.time()
    row = store.read(lambda c: c.execute(
        "SELECT needs_attention FROM reminders WHERE id=?", (reminder_id,)).fetchone())
    store.write(lambda c: c.execute(
        "UPDATE reminders SET nag_count=nag_count+1, last_nag_ts=?, needs_attention=1 WHERE id=?",
        (fired_ts, reminder_id)))
    if row is not None and not row["needs_attention"]:
        _log_event(reminder_id, "nori", None, "flagged",
                  changes={"needs_attention": {"old": 0, "new": 1}}, note="nagged again")


def mark_missed(reminder_id: int, row: dict, *, fired_ts: float | None = None) -> dict:
    """A due occurrence that hit the end-of-day cutoff still pending --
    logged 'missed' (distinct from 'done'), then advanced/closed exactly
    like close() does, just with a different outcome recorded."""
    fired_ts = fired_ts if fired_ts is not None else time.time()
    _log_event(reminder_id, "nori", None, "missed")
    if row["recur_type"]:
        next_due = recurrence.compute_next(row["recur_type"], time_hour=row["due_hour"],
                                           time_minute=row["due_minute"], weekday=row["recur_weekday"],
                                           month_day=row["recur_month_day"],
                                           week_ordinal=row["recur_week_ordinal"], after=fired_ts,
                                           tz=usertime.zone_for(row["user_id"]))
        store.write(lambda c: c.execute(
            "UPDATE reminders SET occurrence_status='pending', nag_count=0, last_nag_ts=NULL, "
            "next_due_ts=? WHERE id=?", (next_due, reminder_id)))
        return {"recurred": True, "next_due_ts": next_due}
    store.write(lambda c: c.execute(
        "UPDATE reminders SET status='closed', occurrence_status='missed' WHERE id=?", (reminder_id,)))
    return {"recurred": False}


# ── tool registration ────────────────────────────────────────────────────
def _reminder_add_impl(session: dict, **kw) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return add(session, created_by_type=actor, created_by_peer_name=actor_peer_name, **kw)


def _reminder_update_impl(session: dict, *, reminder_id: int, **kw) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return update(session, reminder_id, actor=actor, actor_peer_name=actor_peer_name, **kw)


def _reminder_close_impl(session: dict, *, reminder_id: int, note: str | None = None) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return close(session, reminder_id, note=note, actor=actor, actor_peer_name=actor_peer_name)


def _reminder_record_progress_impl(session: dict, *, reminder_id: int, note: str | None = None,
                                   status: str | None = None) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return record_progress(session, reminder_id, note=note, status=status, actor=actor,
                           actor_peer_name=actor_peer_name)


def _reminder_list_impl(session: dict, *, status: str | None = "active", category: str | None = None) -> dict:
    return list_for_user(session, status=status, category=category)


def _reminder_set_needs_attention_impl(session: dict, *, reminder_id: int, needs_attention: bool) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return set_needs_attention(session, reminder_id, needs_attention, actor=actor,
                               actor_peer_name=actor_peer_name)


def _register_tools() -> None:
    import tools  # local: same reasoning as every other subsystem module in this app

    recur_props = {
        "recur_type": {"type": "string", "enum": list(RECUR_TYPES),
                       "description": "Omit for a one-off reminder (pass once_date instead). "
                                     "'time' repeats daily. 'weekly' repeats on one weekday. "
                                     "'monthly_day' repeats on a day-of-month. 'monthly_weekday' "
                                     "repeats on the Nth weekday of the month."},
        "once_date": {"type": "string", "description": "Required when recur_type is omitted -- "
                                                        "'today', 'tomorrow', an ISO date, etc."},
        "recur_weekday": {"type": "integer", "description": "0=Monday..6=Sunday. For 'weekly' and "
                                                             "'monthly_weekday'."},
        "recur_month_day": {"type": "integer", "description": "1-31. For 'monthly_day' -- clamped to "
                                                               "the last day in a shorter month."},
        "recur_week_ordinal": {"type": "integer", "enum": [1, 2, 3, 4, -1],
                               "description": "For 'monthly_weekday': 1st-4th occurrence, or -1 for "
                                             "the last (handles months with only four of that "
                                             "weekday, and 'last Friday' when there's a real 5th)."},
    }

    tools.register(tools.Tool(
        "reminder_add",
        {"type": "function", "function": {
            "name": "reminder_add",
            "description": "Add a reminder -- like a task, but urgent: presented at due_hour/"
                           "due_minute and nagged about a few times until closed, rather than "
                           "sitting quietly on the board.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string"},
                "body": {"type": "string", "description": "Optional longer detail."},
                "category": {"type": "string", "description": "Not a fixed list -- call "
                                                                "category_list(domain='reminder') if "
                                                                "unsure what exists, or category_add."},
                "due_hour": {"type": "integer", "description": "0-23, local time."},
                "due_minute": {"type": "integer", "description": "0-59."},
                **recur_props,
                "nag_interval_min": {"type": "integer",
                                    "description": f"Minutes between nags (default {DEFAULT_NAG_INTERVAL_MIN})."},
                "nag_max_count": {"type": "integer",
                                 "description": f"Total contacts about one occurrence, including the "
                                               f"first at the due time (default {DEFAULT_NAG_MAX_COUNT})."}},
                "required": ["name", "category", "due_hour", "due_minute"]}}},
        _reminder_add_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "reminder_update",
        {"type": "function", "function": {
            "name": "reminder_update",
            "description": "Change one of his existing reminders -- only pass the fields you're "
                           "actually changing.",
            "parameters": {"type": "object", "properties": {
                "reminder_id": {"type": "integer"},
                "name": {"type": "string"}, "body": {"type": "string"}, "category": {"type": "string"},
                "due_hour": {"type": "integer"}, "due_minute": {"type": "integer"},
                **recur_props,
                "nag_interval_min": {"type": "integer"}, "nag_max_count": {"type": "integer"}},
                "required": ["reminder_id"]}}},
        _reminder_update_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "reminder_close",
        {"type": "function", "function": {
            "name": "reminder_close",
            "description": "Mark the current occurrence done -- never deletes. A recurring reminder "
                           "advances to its next occurrence and stays active; a one-off closes for good.",
            "parameters": {"type": "object", "properties": {
                "reminder_id": {"type": "integer"},
                "note": {"type": "string", "description": "Optional -- context for the history."}},
                "required": ["reminder_id"]}}},
        _reminder_close_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "reminder_record_progress",
        {"type": "function", "function": {
            "name": "reminder_record_progress",
            "description": "Record where things stand on a reminder mid-cycle, without closing it -- "
                           "what he said, or a quick status. Distinct from reminder_close (which ends "
                           "the occurrence).",
            "parameters": {"type": "object", "properties": {
                "reminder_id": {"type": "integer"},
                "note": {"type": "string"},
                "status": {"type": "string", "enum": list(PROGRESS_STATUSES)}},
                "required": ["reminder_id"]}}},
        _reminder_record_progress_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "reminder_list",
        {"type": "function", "function": {
            "name": "reminder_list",
            "description": "List his reminders -- active by default, so you know what already "
                           "exists (and each one's real id) before adding a duplicate.",
            "parameters": {"type": "object", "properties": {
                "status": {"type": "string", "enum": list(STATUSES),
                          "description": "Omit for active reminders (the default)."},
                "category": {"type": "string"}}}}},
        _reminder_list_impl, min_role="member", data_scope="self", risk_tier="A"))

    tools.register(tools.Tool(
        "reminder_set_needs_attention",
        {"type": "function", "function": {
            "name": "reminder_set_needs_attention",
            "description": "Mark a reminder as needing his attention again, or as already seen. New "
                           "reminders start needing attention automatically, and every fresh nag "
                           "re-raises it on its own -- this is for the exceptional case beyond that.",
            "parameters": {"type": "object", "properties": {
                "reminder_id": {"type": "integer"}, "needs_attention": {"type": "boolean"}},
                "required": ["reminder_id", "needs_attention"]}}},
        _reminder_set_needs_attention_impl, min_role="member", data_scope="self", risk_tier="B"))


_register_tools()


def register_peer_actions() -> None:
    """Deferred to server.py's own main(), same reasoning as memory.py's/
    homeassistant.py's/schedules.py's identical functions. All six
    tools on the STANDARD trust ladder, no full_trust_only tier -- his
    own instruction (2026-09-16): "peers get access to everything just
    built... gated on trust level, same pattern as the settings tool
    and MCP toggles," extended the same way for reminder_set_needs_
    attention (2026-09-17, his own follow-up: "peers can presumably set
    the flag too... follow the same trust gating"). No destructive-
    operation concern the way note_delete has one -- close() and
    record_progress() are never a hard delete, so there's nothing here
    for this registration to soften."""
    import peers
    peers.register_peer_requestable("reminder_add")
    peers.register_peer_requestable("reminder_update")
    peers.register_peer_requestable("reminder_close")
    peers.register_peer_requestable("reminder_record_progress")
    peers.register_peer_requestable("reminder_list")
    peers.register_peer_requestable("reminder_set_needs_attention")


# ── proactive-ping signal (2026-09-26) ──────────────────────────────────
def scheduler_signal(user_id: int) -> str | None:
    """The operator's own reminders that fired, ran out their nag budget, and were
    never acted on -- NOT a re-announcement of one still firing on its
    own schedule (that would just be the same nag twice, the opposite of
    what he asked for). Scoped deliberately to NON-recurring reminders:
    mark_missed() gives those a stable, queryable 'missed' state
    (status='closed', occurrence_status='missed'); a recurring reminder
    that missed once immediately re-arms for its next occurrence, so
    there's nothing stable left to point at -- only its event log has a
    record, and nagging about a past miss on something already re-fired
    normally would be noise, not help. Widen this if he wants recurring
    misses included too; start conservative."""
    rows = store.read(lambda c: c.execute(
        "SELECT name FROM reminders WHERE user_id=? AND status='closed' AND occurrence_status='missed' "
        "ORDER BY next_due_ts", (user_id,)).fetchall())
    if not rows:
        return None
    names = ", ".join(r["name"] for r in rows[:5])
    if len(rows) > 5:
        names += f", and {len(rows) - 5} more"
    return f"reminders that were missed and never followed up: {names}"


def register_scheduler_signal() -> None:
    """NOT auto-called at import time the way household.py/meals.py/
    notes.py/email_calendar.py register themselves -- scheduler.py
    already imports THIS module at its own top level (for
    _check_due_reminders), so `import scheduler` here during THIS
    module's own import would hit scheduler while it's still mid-import,
    before register_signal is even defined yet: a real circular import,
    not a hypothetical one (found live writing this). Called once instead
    from scheduler.py's own _register_builtin_signals(), after scheduler
    has fully finished defining itself."""
    import scheduler
    scheduler.register_signal(scheduler_signal, key="missed_reminders",
                              label="Reminders that were missed and never followed up",
                              tier="urgent", cooldown_seconds=4 * 3600)

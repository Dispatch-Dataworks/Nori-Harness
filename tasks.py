# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The task/board system (2026-09-16) -- the first of WISHLIST.md's "card
surface" types actually built (2026-09-11 entry: note/task/reminder/
running-topic), scoped to just tasks for now, per the operator's own
explicit instruction. This module owns the `tasks`/`task_events` tables.

Distinct from memory.py's existing type='task' memory entries -- checked
before writing a line of this module, per the operator's own request to
report the overlap rather than assume. memory.py's own comment on this:
"a specific standing commitment or todo-shaped thing worth remembering
exists" -- a free-text FACT that something task-shaped is worth
remembering, filed and recalled the same way any other memory type is,
with no due date, priority, recurrence, board presence, or lifecycle of
its own. This module is the structured, board-visible object those
memory entries were always a placeholder for -- real fields, a real
close/reopen lifecycle, a real history. memory.py's own TYPE_HELP entry
for "task" is updated (see that module) to point here now that there's
somewhere real to point to, the same disambiguating treatment
"household"/"meals" already got when their own dedicated systems shipped.

Recurrence reuses recurrence.py verbatim (2026-09-16, operator's own
instruction: "you just built schedule recurrence for the scheduler,
reuse that rather than a second implementation") -- same rule shape,
same catch-up-safe advance-forward-from-now policy schedules.py needed
first. A recurring task's own close() advances due_ts to the next
occurrence instead of ending the task -- see close() below.

History (task_events) holds two different SHAPES of entry, deliberately
-- the operator's own framing: "the history isn't only edits, it's
actions taken against the task." A field edit gets action='updated' with
a `changes` diff (same shape schedule_events already established, reused
rather than invented a third time). A pure ACT -- reminding him about a
task without changing anything, closing/completing one -- gets its own
action label and a free-text `note` instead of (or alongside) a diff.
task_update() is the single entry point for both: called with a real
field change, it's 'updated'; called with ONLY a `note` and no field
actually different, it's 'reminded' -- one tool, two possible outcomes,
because "reminding" isn't really a fourth verb she needs of her own, it's
what an update call with nothing to change but something to say for it
already looks like.

**Peers are wired in** (2026-09-16, his own follow-up answer, once
notes/tasks/reminders/trackers all existed) -- see register_peer_
actions() below, standard trust ladder, same as schedules.py/
homeassistant.py/memory.py. created_by_type/created_by_peer_name and
actor/actor_peer_name already used the same three-way vocabulary
(user/nori/peer) from the start, so this is a pure wiring change --
nothing about the schema or the tools themselves needed to move.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

import recurrence
import catalog
import store
import usertime

PRIORITIES = ("low", "normal", "high")
STATUSES = ("open", "closed")
CATEGORY_DOMAIN = "task"
# Was a fixed 5-tuple here (2026-09-16); widened the same day to
# catalog.py's own managed set the moment the operator asked for her and
# him to be able to create/rename/disable categories themselves -- see
# catalog.py's own module docstring. No CATEGORIES constant survives
# here at all -- validity is always checked live against
# catalog.is_valid(session, CATEGORY_DOMAIN, name), and every caller that
# needs the current list (a UI dropdown, a tool description) calls
# catalog.list_categories(session, CATEGORY_DOMAIN) fresh, never a
# frozen tuple that could go stale the moment she adds one.

# Same fields schedule_events tracks a diff over, adapted to a task's own
# shape -- status is deliberately NOT tracked here (close()/reopen() log
# their own dedicated action, not a generic 'updated' diff on status).
_TRACKED_FIELDS = ("name", "body", "priority", "category", "due_ts", "recur_type",
                  "recur_interval_min", "recur_time_hour", "recur_time_minute")

_UNSET = object()


def parse_when(s, user_id: int) -> float | None:
    """Accepts a plain epoch number, an ISO date/datetime, a few common
    date formats, or a short relative phrase ("today", "tomorrow", "in 3
    days") -- the shapes a model routinely produces for a date/time.
    Zone-aware throughout via usertime (2026-09-18, real bug fixed: this
    used to be naive server-local time via time.localtime/mktime --
    correct for him only by coincidence of where the server happens to
    run). Returns None for an empty/unparseable string, which callers
    treat as "no due date" (task_add) or "clear the due date"
    (task_update) -- named generically (not parse_due), and public
    (2026-09-16, once trackers.py needed the identical parsing for a
    logged entry's own timestamp -- "log this from this morning" is the
    same shape of problem "due tomorrow" already was), not a second copy.

    "today"/"tonight"/"eod"/"tomorrow"/"yesterday" all resolve to
    23:59:00 on their respective day -- correct as a DUE-DATE CEILING
    (a task "due today" means due by end of day), and that's the only
    use this function's callers ever put these relative words to. A
    caller that needs a START-of-period boundary instead (trackers.py's
    own "since today" -- a real bug, "today" as a since= used to mean
    23:59 today, producing an inverted, always-empty range) wants
    parse_since() below, not this."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if not s:
        return None
    low = s.lower()
    now = time.time()
    if low in ("today", "tonight", "eod", "end of day"):
        d = usertime.local_dt(user_id, now).date()
        return usertime.to_epoch(user_id, d.year, d.month, d.day, 23, 59, 0)
    if low == "tomorrow":
        d = usertime.local_dt(user_id, now).date() + timedelta(days=1)
        return usertime.to_epoch(user_id, d.year, d.month, d.day, 23, 59, 0)
    # "yesterday" (2026-09-16, added once trackers.py needed it for a
    # retroactive log -- a due date is rarely in the past, so tasks.py's
    # own original callers never hit this gap) -- same day-boundary
    # anchor as today/tomorrow, for the same reason: one consistent rule
    # per relative word, not a different resolution scheme per caller.
    if low == "yesterday":
        d = usertime.local_dt(user_id, now).date() - timedelta(days=1)
        return usertime.to_epoch(user_id, d.year, d.month, d.day, 23, 59, 0)
    m = re.match(r"in\s+(\d+)\s*(hour|hr|day|week)", low)
    if m:
        mult = {"hour": 1, "hr": 1, "day": 24, "week": 168}[m.group(2)]
        return now + int(m.group(1)) * mult * 3600
    # "N ago" (2026-09-16, same reasoning as "yesterday" just above --
    # trackers.py's own retroactive logging is the first real caller that
    # ever needed a PAST relative phrase, "in N" alone never covered it).
    m = re.match(r"(\d+)\s*(hour|hr|day|week)s?\s+ago", low)
    if m:
        mult = {"hour": 1, "hr": 1, "day": 24, "week": 168}[m.group(2)]
        return now - int(m.group(1)) * mult * 3600
    dt = None
    year_omitted = False
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y/%m/%d", "%m/%d/%Y", "%m/%d"):
            try:
                dt = datetime.strptime(s, fmt)
                year_omitted = "%Y" not in fmt
                break
            except ValueError:
                continue
    if dt is None:
        return None
    def _ts(d: datetime) -> float:
        return usertime.to_epoch(user_id, d.year, d.month, d.day, d.hour, d.minute, d.second)
    this_year = usertime.local_dt(user_id, now).year
    # A format with no year in it (bare "%m/%d") defaults to 1900 --
    # never worth treating as a real timestamp; go straight to guessing
    # this year (or next), same self-correction a sibling application/tasks.py's own
    # parse_when uses for the identical "model got the year wrong" case.
    if year_omitted:
        for yr in (this_year, this_year + 1):
            cand = _ts(dt.replace(year=yr))
            if now - cand < 36 * 3600:
                return cand
        return _ts(dt.replace(year=this_year))
    ts = _ts(dt)
    if now - ts > 36 * 3600:
        for yr in (this_year, this_year + 1):
            try:
                cand = _ts(dt.replace(year=yr))
            except ValueError:
                continue
            if now - cand < 36 * 3600:
                return cand
    return ts


def parse_since(s, user_id: int) -> float | None:
    """Like parse_when, but for a LOWER bound (trackers.py's own "how
    much water since X" query) -- "today"/"this week"/"yesterday" mean
    the START of that period here, not end-of-day the way parse_when's
    due-date-ceiling semantics do. Everything else (an explicit date, "3
    days ago", a plain epoch) means the same instant either way, so it
    falls straight through to parse_when unchanged.

    2026-09-18, real bug this fixes: trackers.history_for_type's own
    since="today" used to go through parse_when, getting 23:59 TODAY as
    the lower bound -- after "now" for all but the last minute of the
    day, so the range was inverted and always empty. Reproduced live
    against his real water-tracker data before this existed."""
    if s is None:
        return None
    low = str(s).strip().lower()
    if low in ("today", "tonight"):
        return usertime.day_start(user_id)
    if low == "yesterday":
        return usertime.day_start(user_id, time.time() - 86400)
    if low in ("this week", "week"):
        return usertime.week_start(user_id)
    return parse_when(s, user_id)


def _validate(session: dict, *, name: str, priority: str, category: str, recur_type: str | None,
             recur_interval_min: int | None, recur_time_hour: int | None,
             recur_time_minute: int | None) -> str | None:
    if not (name or "").strip():
        return "name can't be empty"
    if priority not in PRIORITIES:
        return f"priority must be one of: {', '.join(PRIORITIES)}"
    if not catalog.is_valid(session, CATEGORY_DOMAIN, category):
        return (f"{category!r} isn't a current task category -- check category_list, or "
               f"category_add it first")
    return recurrence.validate(recur_type, recur_interval_min, recur_time_hour, recur_time_minute)


def _log_event(task_id: int, actor: str, actor_peer_name: str | None, action: str,
               changes: dict | None = None, note: str | None = None) -> None:
    import json
    store.write(lambda c: c.execute(
        "INSERT INTO task_events(task_id, ts, actor, actor_peer_name, action, changes, note) "
        "VALUES (?,?,?,?,?,?,?)",
        (task_id, time.time(), actor, actor_peer_name, action,
         json.dumps(changes) if changes else None, note)))


def _diff(old: dict, new: dict) -> dict:
    changes = {}
    for f in _TRACKED_FIELDS:
        if old.get(f) != new.get(f):
            changes[f] = {"old": old.get(f), "new": new.get(f)}
    return changes


def add(session: dict, *, name: str, category: str, body: str | None = None,
       priority: str = "normal", due=None, recur_type: str | None = None,
       recur_interval_min: int | None = None, recur_time_hour: int | None = None,
       recur_time_minute: int | None = None, created_by_type: str = "user",
       created_by_peer_name: str | None = None) -> dict:
    # category has NO default, deliberately (2026-09-16, operator's own
    # instruction: "include it in the tool schema so she sets it when
    # creating, rather than everything defaulting to 'other'") -- the
    # JSON schema's own "required" list is only a prompt-level hint to
    # the model, not something tools.dispatch() enforces on its own; a
    # missing keyword-only argument here raises TypeError, which
    # dispatch() already turns into a real, visible error instead of
    # silently landing on 'other'. Every UI caller (board_task_create_
    # post) already always sends a real value anyway -- a <select> can't
    # submit nothing.
    err = _validate(session, name=name, priority=priority, category=category, recur_type=recur_type,
                    recur_interval_min=recur_interval_min, recur_time_hour=recur_time_hour,
                    recur_time_minute=recur_time_minute)
    if err:
        return {"error": err}
    due_ts = parse_when(due, session["user_id"])
    if due and due_ts is None:
        return {"error": f"couldn't understand due date {due!r}"}
    now = time.time()
    def _w(c):
        return c.execute(
            "INSERT INTO tasks(user_id, name, body, priority, category, due_ts, recur_type, "
            "recur_interval_min, recur_time_hour, recur_time_minute, status, created_by_type, "
            "created_by_peer_name, created_ts) VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,?,?)",
            (session["user_id"], name.strip(), body.strip() if body else None, priority, category,
             due_ts, recur_type, recur_interval_min, recur_time_hour, recur_time_minute,
             created_by_type, created_by_peer_name, now)).lastrowid
    tid = store.write(_w)
    _log_event(tid, created_by_type, created_by_peer_name, "created")
    return {"ok": True, "task_id": tid}


def _get_owned(session: dict, task_id: int) -> dict | None:
    row = store.read(lambda c: c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    if row is None or row["user_id"] != session["user_id"]:
        return None
    return dict(row)


def update(session: dict, task_id: int, *, name=_UNSET, body=_UNSET, priority=_UNSET,
          category=_UNSET, due=_UNSET, recur_type=_UNSET, recur_interval_min=_UNSET,
          recur_time_hour=_UNSET, recur_time_minute=_UNSET, note: str | None = None,
          actor: str = "user", actor_peer_name: str | None = None) -> dict:
    """Every field optional. Called with a real field change, this logs
    action='updated' with the diff. Called with ONLY `note` and no field
    that actually differs, this logs action='reminded' instead -- see the
    module docstring's own explanation of why that's not a separate tool.
    A call with neither a real change nor a note is a true no-op: nothing
    to log, nothing happened."""
    row = _get_owned(session, task_id)
    if row is None:
        return {"error": "no such task"}
    merged = dict(row)
    for key, val in (("name", name), ("body", body), ("priority", priority), ("category", category),
                     ("recur_type", recur_type), ("recur_interval_min", recur_interval_min),
                     ("recur_time_hour", recur_time_hour), ("recur_time_minute", recur_time_minute)):
        if val is not _UNSET:
            merged[key] = val
    if due is not _UNSET:
        due_ts = parse_when(due, session["user_id"])
        if due and due_ts is None:
            return {"error": f"couldn't understand due date {due!r}"}
        merged["due_ts"] = due_ts
    err = _validate(session, name=merged["name"], priority=merged["priority"], category=merged["category"],
                    recur_type=merged["recur_type"], recur_interval_min=merged["recur_interval_min"],
                    recur_time_hour=merged["recur_time_hour"], recur_time_minute=merged["recur_time_minute"])
    if err:
        return {"error": err}
    final = {**merged, "name": merged["name"].strip(),
            "body": (merged["body"].strip() if merged.get("body") else None)}
    store.write(lambda c: c.execute(
        "UPDATE tasks SET name=?, body=?, priority=?, category=?, due_ts=?, recur_type=?, "
        "recur_interval_min=?, recur_time_hour=?, recur_time_minute=? WHERE id=?",
        (final["name"], final["body"], final["priority"], final["category"], final["due_ts"],
         final["recur_type"], final["recur_interval_min"], final["recur_time_hour"],
         final["recur_time_minute"], task_id)))
    changes = _diff(row, final)
    if changes:
        _log_event(task_id, actor, actor_peer_name, "updated", changes=changes, note=note)
    elif note:
        _log_event(task_id, actor, actor_peer_name, "reminded", note=note)
    return {"ok": True}


def close(session: dict, task_id: int, *, note: str | None = None,
         actor: str = "user", actor_peer_name: str | None = None) -> dict:
    """Close, never delete -- the operator's own explicit rule; there is
    no remove/delete path anywhere in this module. A task WITHOUT
    recurrence closes for good: status='closed', hidden from the board,
    still fully visible in the settings history. A task WITH recurrence
    logs action='completed' instead, advances due_ts to the next
    occurrence (recurrence.compute_next, the same catch-up-safe "relative
    to now, never the missed slot" policy schedules.py's own fires use),
    and stays status='open' -- closing one occurrence of a recurring task
    is completing it, not ending it. To stop a recurring task for good,
    clear its recurrence first (update(recur_type=None)), then close it."""
    row = _get_owned(session, task_id)
    if row is None:
        return {"error": "no such task"}
    if row["status"] == "closed":
        return {"error": "already closed"}
    now = time.time()
    if row["recur_type"]:
        next_due = recurrence.compute_next(row["recur_type"], row["recur_interval_min"],
                                           row["recur_time_hour"], row["recur_time_minute"], after=now,
                                           tz=usertime.zone_for(session["user_id"]))
        store.write(lambda c: c.execute("UPDATE tasks SET due_ts=? WHERE id=?", (next_due, task_id)))
        _log_event(task_id, actor, actor_peer_name, "completed", note=note)
        return {"ok": True, "recurred": True, "next_due_ts": next_due}
    store.write(lambda c: c.execute("UPDATE tasks SET status='closed', closed_ts=? WHERE id=?",
                                    (now, task_id)))
    _log_event(task_id, actor, actor_peer_name, "closed", note=note)
    return {"ok": True, "recurred": False}


def set_needs_attention(session: dict, task_id: int, needs_attention: bool, *, actor: str = "user",
                        actor_peer_name: str | None = None) -> dict:
    """A new task starts needing his attention (schema default 1);
    viewing its full-screen board detail page is what actually clears
    it, not merely being rendered as a card (2026-09-17, his own design
    point: "everything marks read on page load" would make the feature
    do nothing). Nori (or a trusted peer) can set it either direction
    herself via this same function -- re-flagging something already
    seen is a real, logged action ("Nori marked this needs-attention
    again"), not just a one-way clear."""
    row = _get_owned(session, task_id)
    if row is None:
        return {"error": "no such task"}
    new_val = 1 if needs_attention else 0
    if row["needs_attention"] == new_val:
        return {"ok": True}
    store.write(lambda c: c.execute("UPDATE tasks SET needs_attention=? WHERE id=?", (new_val, task_id)))
    _log_event(task_id, actor, actor_peer_name, "flagged",
              changes={"needs_attention": {"old": row["needs_attention"], "new": new_val}})
    return {"ok": True}


def list_for_user(session: dict, *, status: str | None = "open", category: str | None = None) -> dict:
    """status='open' (the board's own default -- closed tasks are hidden
    there), 'closed', or None for everything (the settings page's own
    "see all tasks including closed ones" requirement). category=None
    (the default) means every category -- the settings page's own filter
    (2026-09-16, operator's own addition) passes a real one to narrow."""
    if status is not None and status not in STATUSES:
        return {"error": f"status must be one of: {', '.join(STATUSES)}, or omitted for all"}
    # No catalog.is_valid() check here, deliberately -- a filter by a
    # DISABLED (or even long-gone) category name must still work, so he
    # can still find whatever's already filed under it; disabling only
    # ever gates NEW items, never reads of existing ones.
    where = ["user_id=?"]
    params = [session["user_id"]]
    if status is not None:
        where.append("status=?")
        params.append(status)
    if category is not None:
        where.append("category=?")
        params.append(category)
    rows = store.read(lambda c: c.execute(
        f"SELECT * FROM tasks WHERE {' AND '.join(where)} "
        "ORDER BY (due_ts IS NULL), due_ts, created_ts", tuple(params)).fetchall())
    return {"tasks": [dict(r) for r in rows]}


def get_for_user(session: dict, task_id: int) -> dict | None:
    return _get_owned(session, task_id)


def history_for(task_id: int, limit: int = 50) -> list[dict]:
    """The action+edit trail -- newest first, same convention schedules.
    history_for already established. `changes` comes back parsed as a
    real dict, {} for a pure-action row (created/closed/completed/
    reminded) that never carried one."""
    import json
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM task_events WHERE task_id=? ORDER BY ts DESC LIMIT ?",
        (task_id, limit)).fetchall())
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["changes"] = json.loads(d["changes"]) if d.get("changes") else {}
        except (ValueError, TypeError):
            d["changes"] = {}
        out.append(d)
    return out


# ── tool registration ────────────────────────────────────────────────────
def _task_add_impl(session: dict, **kw) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return add(session, created_by_type=actor, created_by_peer_name=actor_peer_name, **kw)


def _task_update_impl(session: dict, *, task_id: int, **kw) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return update(session, task_id, actor=actor, actor_peer_name=actor_peer_name, **kw)


def _task_close_impl(session: dict, *, task_id: int, note: str | None = None) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return close(session, task_id, note=note, actor=actor, actor_peer_name=actor_peer_name)


def _task_list_impl(session: dict, *, status: str | None = "open", category: str | None = None) -> dict:
    return list_for_user(session, status=status, category=category)


def _task_set_needs_attention_impl(session: dict, *, task_id: int, needs_attention: bool) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return set_needs_attention(session, task_id, needs_attention, actor=actor, actor_peer_name=actor_peer_name)


def _register_tools() -> None:
    import tools  # local: same reasoning as household.py/memory.py/schedules.py

    recur_props = {
        "recur_type": {"type": "string", "enum": list(recurrence.TYPES),
                       "description": "Omit for a one-off task. 'interval' repeats every N minutes "
                                     "after each completion; 'time' repeats daily at a fixed time."},
        "recur_interval_min": {"type": "integer",
                               "description": f"For recur_type='interval': minutes between "
                                             f"occurrences ({recurrence.MIN_INTERVAL_MIN}-"
                                             f"{recurrence.MAX_INTERVAL_MIN})."},
        "recur_time_hour": {"type": "integer", "description": "For recur_type='time': 0-23, local time."},
        "recur_time_minute": {"type": "integer", "description": "For recur_type='time': 0-59."},
    }

    tools.register(tools.Tool(
        "task_add",
        {"type": "function", "function": {
            "name": "task_add",
            "description": "Add a task to his board. Shows up as an open card until closed.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string"},
                "body": {"type": "string", "description": "Optional longer detail."},
                "priority": {"type": "string", "enum": list(PRIORITIES)},
                "category": {"type": "string",
                            "description": "Pick the one that actually fits -- don't default to "
                                           "'other' without thinking about it. Not a fixed list -- "
                                           "call category_list(domain='task') if you're not sure "
                                           "what currently exists, or category_add to create one."},
                "due": {"type": "string", "description": "Optional. 'today', 'tomorrow', 'in 3 days', "
                                                          "or an ISO date/datetime."},
                **recur_props},
                # category is required, deliberately (2026-09-16, operator's own
                # instruction) -- in the schema so she actually sets it when
                # creating, rather than everything silently landing on 'other'.
                "required": ["name", "category"]}}},
        _task_add_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "task_update",
        {"type": "function", "function": {
            "name": "task_update",
            "description": "Change one of his existing tasks -- only pass the fields you're actually "
                           "changing. Also how you record reminding him about one: call with `note` "
                           "describing what you told him and no other field, and it logs as a reminder "
                           "in the task's own history rather than an edit.",
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "integer"},
                "name": {"type": "string"}, "body": {"type": "string"},
                "priority": {"type": "string", "enum": list(PRIORITIES)},
                "category": {"type": "string"},
                "due": {"type": "string", "description": "New due date, same formats as task_add, or "
                                                          "an empty string to clear it."},
                **recur_props,
                "note": {"type": "string", "description": "What to record in this task's own history "
                                                           "-- e.g. what you just told him. Required to "
                                                           "log a reminder; optional context on a real "
                                                           "field change too."}},
                "required": ["task_id"]}}},
        _task_update_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "task_close",
        {"type": "function", "function": {
            "name": "task_close",
            "description": "Close a task -- never deletes it, just takes it off the board. A "
                           "recurring task instead advances to its next occurrence and stays open; "
                           "see task_update to stop the recurrence first if you want it to actually end.",
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "integer"},
                "note": {"type": "string", "description": "Optional -- context for the history, e.g. "
                                                           "how it actually got done."}},
                "required": ["task_id"]}}},
        _task_close_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "task_list",
        {"type": "function", "function": {
            "name": "task_list",
            "description": "List his tasks -- open by default, so you know what's already on the "
                           "board (and each one's real id) before adding a duplicate or trying to "
                           "update/close one.",
            "parameters": {"type": "object", "properties": {
                "status": {"type": "string", "enum": list(STATUSES),
                          "description": "Omit for open tasks (the board's own default)."},
                "category": {"type": "string", "description": "Omit for every category."}}}}},
        _task_list_impl, min_role="member", data_scope="self", risk_tier="A"))

    tools.register(tools.Tool(
        "task_set_needs_attention",
        {"type": "function", "function": {
            "name": "task_set_needs_attention",
            "description": "Mark a task as needing his attention again, or as already seen. New "
                           "tasks start needing attention automatically; this is for the exceptional "
                           "case -- e.g. re-flagging something he dismissed too soon, or clearing one "
                           "he's clearly already dealt with in conversation.",
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "integer"}, "needs_attention": {"type": "boolean"}},
                "required": ["task_id", "needs_attention"]}}},
        _task_set_needs_attention_impl, min_role="member", data_scope="self", risk_tier="B"))


_register_tools()


def register_peer_actions() -> None:
    """Deferred to server.py's own main(), same reasoning as memory.py's/
    homeassistant.py's/schedules.py's identical functions. All five
    tools on the STANDARD trust ladder, no full_trust_only tier -- his
    own instruction (2026-09-16): "peers get access to everything just
    built... gated on trust level, same pattern as the settings tool
    and MCP toggles," extended the same way for task_set_needs_attention
    (2026-09-17, his own follow-up: "peers can presumably set the flag
    too... follow the same trust gating"). No destructive-operation
    concern here the way note_delete has one -- a task is never really
    deleted, only close()d, so there's nothing for this registration to
    soften."""
    import peers
    peers.register_peer_requestable("task_add")
    peers.register_peer_requestable("task_update")
    peers.register_peer_requestable("task_close")
    peers.register_peer_requestable("task_list")
    peers.register_peer_requestable("task_set_needs_attention")


# ── proactive-ping signal (2026-09-26) ──────────────────────────────────
def scheduler_signal(user_id: int) -> str | None:
    """An overdue task -- past its own due_ts, still open. NOT "due
    Friday": the operator's own line ("an overdue task is; a task due Friday
    isn't") is the whole design of this threshold -- only genuinely past-
    due tasks are worth an unprompted nag."""
    rows = store.read(lambda c: c.execute(
        "SELECT name FROM tasks WHERE user_id=? AND status='open' AND due_ts IS NOT NULL AND due_ts<? "
        "ORDER BY due_ts", (user_id, time.time())).fetchall())
    if not rows:
        return None
    names = ", ".join(r["name"] for r in rows[:5])
    if len(rows) > 5:
        names += f", and {len(rows) - 5} more"
    return f"overdue tasks: {names}"


import scheduler  # local-at-module-bottom on purpose: registers this module's signal once, at import
scheduler.register_signal(scheduler_signal, key="overdue_tasks", label="Overdue tasks",
                          tier="urgent", cooldown_seconds=6 * 3600)

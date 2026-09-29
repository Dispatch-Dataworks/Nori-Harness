# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""General-purpose scheduler entries (2026-09-15, operator's own settled
design -- see the conversation between him and Nori that started this,
and his own follow-up messages resolving the schema/notification/peer-
parity questions). This module owns the `schedules`/`schedule_runs`
tables and their CRUD -- the actual due-check and turn-firing logic lives
in scheduler.py itself (same reasoning household.py's scheduler_signal
documents: keeping chat/turns/conversation imports out of this module
avoids a new circular-import path, and scheduler.py already has all of
those imported for its own proactive-ping cycle).

Each entry has a name, a free-text instruction (no allowlist -- "the
instruction is stored input that executes later," the operator's own
characterization), an optional required_tool (checked live at fire time,
never just recorded -- see scheduler.py's _fire_schedule), a deliver_to
routing (him / a named peer / both / neither), and either an interval or
a specific daily time. Peers at full trust get the exact same create/edit
tools Nori does (register_peer_actions below) -- the operator's own call,
same standard trust ladder every other HA/settings tool this session
built already uses, no stricter tier for this one.

Edit history (2026-09-15, operator's own follow-up): every create/update/
enable/disable/delete writes a `schedule_events` row -- same actor-tagged
append-only event log shape memory.py's memory_events already
established (see that module's own _log/_actor_for), reused rather than
invented a third time. Because a peer can edit a schedule Nori (or he)
authored, and vice versa, actor + actor_peer_name (WHO), ts (WHEN), and a
per-field changes diff (WHAT) are what makes that history actually
answer "who shaped this into what it now says," not just "when was it
last touched."
"""
from __future__ import annotations

import time

import recurrence
import store
import usertime

# Re-exported from recurrence.py (2026-09-16 extraction -- see that
# module's own docstring) so nothing that already reads schedules.
# SCHEDULE_TYPES/MIN_INTERVAL_MIN/MAX_INTERVAL_MIN breaks.
SCHEDULE_TYPES = recurrence.TYPES
MIN_INTERVAL_MIN = recurrence.MIN_INTERVAL_MIN
MAX_INTERVAL_MIN = recurrence.MAX_INTERVAL_MIN

DELIVER_TO_VALUES = ("user", "peer", "both", "none")
CREATED_BY_TYPES = ("user", "nori", "peer")

# The fields a schedule_events diff actually tracks -- every column an
# edit can change. Deliberately excludes bookkeeping columns
# (next_run_ts, last_run_ts, last_status, last_error) that change on
# every fire, not on an edit -- those belong to schedule_runs, not to
# this trail.
_TRACKED_FIELDS = ("name", "instruction", "required_tool", "deliver_to", "deliver_peer_id",
                  "schedule_type", "interval_min", "time_hour", "time_minute", "enabled")

_UNSET = object()


def _diff(old: dict, new: dict) -> dict:
    """{field: {"old", "new"}} for every _TRACKED_FIELDS entry that
    actually changed -- the WHAT half of the trail. Empty when nothing
    tracked changed (e.g. an edit call that only touched next_run_ts
    bookkeeping), so a no-op edit doesn't add a hollow event."""
    changes = {}
    for f in _TRACKED_FIELDS:
        if old.get(f) != new.get(f):
            changes[f] = {"old": old.get(f), "new": new.get(f)}
    return changes


def _log_event(schedule_id: int, actor: str, actor_peer_name: str | None, action: str,
               changes: dict | None = None, note: str | None = None) -> None:
    import json
    store.write(lambda c: c.execute(
        "INSERT INTO schedule_events(schedule_id, ts, actor, actor_peer_name, action, changes, note) "
        "VALUES (?,?,?,?,?,?,?)",
        (schedule_id, time.time(), actor, actor_peer_name, action,
         json.dumps(changes) if changes else None, note)))


def _compute_next_run(schedule_type: str, interval_min: int | None, time_hour: int | None,
                      time_minute: int | None, *, after: float, user_id: int) -> float:
    return recurrence.compute_next(schedule_type, interval_min, time_hour, time_minute, after=after,
                                   tz=usertime.zone_for(user_id))


def _validate(*, name: str, instruction: str, required_tool: str | None, deliver_to: str,
             deliver_peer_id: int | None, schedule_type: str, interval_min: int | None,
             time_hour: int | None, time_minute: int | None) -> str | None:
    import tools  # local: same reasoning as every other subsystem module in this app
    if not (name or "").strip():
        return "name can't be empty"
    if not (instruction or "").strip():
        return "instruction can't be empty"
    if deliver_to not in DELIVER_TO_VALUES:
        return f"deliver_to must be one of: {', '.join(DELIVER_TO_VALUES)}"
    if deliver_to in ("peer", "both"):
        if not deliver_peer_id:
            return "deliver_to requires a peer -- pick which one"
        import peers
        if peers.get_peer(deliver_peer_id) is None:
            return "no such peer to deliver to"
    # A schedule's own schedule_type is mandatory (never "no recurrence"),
    # unlike a task's optional recur_type -- checked here, before handing
    # off to recurrence.validate() for the shared interval/time shape
    # check both this and tasks.py rely on.
    if schedule_type not in SCHEDULE_TYPES:
        return f"schedule_type must be one of: {', '.join(SCHEDULE_TYPES)}"
    err = recurrence.validate(schedule_type, interval_min, time_hour, time_minute)
    if err:
        return err
    # Existence only, not availability -- a tool that exists but is
    # currently disabled/unreachable is a live, always-re-checked
    # condition (scheduler.py's own preflight before every fire), not a
    # reason to refuse creating the schedule in the first place; a typo'd
    # tool name that never existed at all is the one thing worth catching
    # here.
    if required_tool and tools.schema_for(required_tool) is None:
        return f"no such tool: {required_tool!r} -- check the name"
    return None


def create(session: dict, *, name: str, instruction: str, required_tool: str | None = None,
          deliver_to: str = "user", deliver_peer_id: int | None = None,
          schedule_type: str, interval_min: int | None = None,
          time_hour: int | None = None, time_minute: int | None = None,
          created_by_type: str = "user", created_by_peer_name: str | None = None) -> dict:
    err = _validate(name=name, instruction=instruction, required_tool=required_tool,
                    deliver_to=deliver_to, deliver_peer_id=deliver_peer_id,
                    schedule_type=schedule_type, interval_min=interval_min,
                    time_hour=time_hour, time_minute=time_minute)
    if err:
        return {"error": err}
    now = time.time()
    next_run = _compute_next_run(schedule_type, interval_min, time_hour, time_minute, after=now,
                                 user_id=session["user_id"])
    def _w(c):
        return c.execute(
            "INSERT INTO schedules(user_id, name, instruction, required_tool, deliver_to, "
            "deliver_peer_id, schedule_type, interval_min, time_hour, time_minute, "
            "created_by_type, created_by_peer_name, enabled, created_ts, next_run_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (session["user_id"], name.strip(), instruction.strip(),
             required_tool.strip() if required_tool else None,
             deliver_to, deliver_peer_id, schedule_type, interval_min, time_hour, time_minute,
             created_by_type, created_by_peer_name, now, next_run)).lastrowid
    sid = store.write(_w)
    _log_event(sid, created_by_type, created_by_peer_name, "created")
    return {"ok": True, "schedule_id": sid, "next_run_ts": next_run}


def _get_owned(session: dict, schedule_id: int) -> dict | None:
    row = store.read(lambda c: c.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone())
    if row is None or row["user_id"] != session["user_id"]:
        return None
    return dict(row)


def edit(session: dict, schedule_id: int, *, name=_UNSET, instruction=_UNSET,
        required_tool=_UNSET, deliver_to=_UNSET, deliver_peer_id=_UNSET,
        schedule_type=_UNSET, interval_min=_UNSET, time_hour=_UNSET, time_minute=_UNSET,
        enabled=_UNSET, actor: str = "user", actor_peer_name: str | None = None) -> dict:
    """Every field optional -- only what's actually passed changes. Editing
    the schedule's own timing (schedule_type/interval_min/time_hour/
    time_minute) recomputes next_run_ts from now, same as a fresh create,
    since the old due time no longer means anything once the shape of the
    schedule itself changed."""
    row = _get_owned(session, schedule_id)
    if row is None:
        return {"error": "no such schedule"}
    merged = dict(row)
    for key, val in (("name", name), ("instruction", instruction), ("required_tool", required_tool),
                     ("deliver_to", deliver_to), ("deliver_peer_id", deliver_peer_id),
                     ("schedule_type", schedule_type), ("interval_min", interval_min),
                     ("time_hour", time_hour), ("time_minute", time_minute)):
        if val is not _UNSET:
            merged[key] = val
    err = _validate(name=merged["name"], instruction=merged["instruction"],
                    required_tool=merged["required_tool"], deliver_to=merged["deliver_to"],
                    deliver_peer_id=merged["deliver_peer_id"], schedule_type=merged["schedule_type"],
                    interval_min=merged["interval_min"], time_hour=merged["time_hour"],
                    time_minute=merged["time_minute"])
    if err:
        return {"error": err}
    retime = any(v is not _UNSET for v in (schedule_type, interval_min, time_hour, time_minute))
    next_run = (_compute_next_run(merged["schedule_type"], merged["interval_min"], merged["time_hour"],
                                  merged["time_minute"], after=time.time(), user_id=session["user_id"])
               if retime else row["next_run_ts"])
    new_enabled = row["enabled"] if enabled is _UNSET else (1 if enabled else 0)
    final = {**merged, "name": merged["name"].strip(), "instruction": merged["instruction"].strip(),
            "required_tool": merged["required_tool"].strip() if merged["required_tool"] else None,
            "enabled": new_enabled}
    store.write(lambda c: c.execute(
        "UPDATE schedules SET name=?, instruction=?, required_tool=?, deliver_to=?, deliver_peer_id=?, "
        "schedule_type=?, interval_min=?, time_hour=?, time_minute=?, enabled=?, next_run_ts=? WHERE id=?",
        (final["name"], final["instruction"], final["required_tool"], final["deliver_to"],
         final["deliver_peer_id"], final["schedule_type"], final["interval_min"], final["time_hour"],
         final["time_minute"], final["enabled"], next_run, schedule_id)))
    changes = _diff(row, final)
    if changes:
        _log_event(schedule_id, actor, actor_peer_name, "updated", changes=changes)
    return {"ok": True}


def set_enabled(session: dict, schedule_id: int, enabled: bool, *,
                actor: str = "user", actor_peer_name: str | None = None) -> dict:
    row = _get_owned(session, schedule_id)
    if row is None:
        return {"error": "no such schedule"}
    new_val = 1 if enabled else 0
    if row["enabled"] == new_val:
        return {"ok": True}  # no-op -- nothing to log
    store.write(lambda c: c.execute("UPDATE schedules SET enabled=? WHERE id=?", (new_val, schedule_id)))
    _log_event(schedule_id, actor, actor_peer_name, "enabled" if enabled else "disabled",
              changes={"enabled": {"old": row["enabled"], "new": new_val}})
    return {"ok": True}


def delete(session: dict, schedule_id: int, *,
          actor: str = "user", actor_peer_name: str | None = None) -> dict:
    row = _get_owned(session, schedule_id)
    if row is None:
        return {"error": "no such schedule"}
    # Logged BEFORE the delete, and schedule_runs/schedule_events rows are
    # left in place afterward, deliberately -- the operator's own
    # requirement is that a run's full instruction, and the edit trail,
    # stay recoverable regardless of what happens to the schedule later; a
    # dangling schedule_id there is the historical record, not an error.
    _log_event(schedule_id, actor, actor_peer_name, "deleted", note=f"deleted \"{row['name']}\"")
    store.write(lambda c: c.execute("DELETE FROM schedules WHERE id=?", (schedule_id,)))
    return {"ok": True}


def list_for_user(session: dict) -> dict:
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM schedules WHERE user_id=? ORDER BY next_run_ts", (session["user_id"],)).fetchall())
    return {"schedules": [dict(r) for r in rows]}


def get_for_user(session: dict, schedule_id: int) -> dict | None:
    return _get_owned(session, schedule_id)


def recent_runs(schedule_id: int, limit: int = 20) -> list[dict]:
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM schedule_runs WHERE schedule_id=? ORDER BY ts DESC LIMIT ?",
        (schedule_id, limit)).fetchall())
    return [dict(r) for r in rows]


def history_for(schedule_id: int, limit: int = 50) -> list[dict]:
    """The edit trail (2026-09-15, operator's own follow-up ask) -- newest
    first, same read-order convention recent_runs above already uses.
    Each row's `changes` (JSON in the DB) comes back parsed as a real
    dict, {} when there wasn't one (created/deleted rows don't carry a
    per-field diff -- see _log_event's own callers)."""
    import json
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM schedule_events WHERE schedule_id=? ORDER BY ts DESC LIMIT ?",
        (schedule_id, limit)).fetchall())
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["changes"] = json.loads(d["changes"]) if d.get("changes") else {}
        except (ValueError, TypeError):
            d["changes"] = {}
        out.append(d)
    return out


# ── used by scheduler.py's own due-check (deliberately NOT wired through
# scheduler.register_signal -- that mechanism is gated by ping_enabled/
# ping_window/recently_active, and the operator was explicit that
# scheduled tasks fire regardless of all three) ─────────────────────────
def due_schedules(user_id: int, now: float | None = None) -> list[dict]:
    now = now if now is not None else time.time()
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM schedules WHERE user_id=? AND enabled=1 AND next_run_ts<=?",
        (user_id, now)).fetchall())
    return [dict(r) for r in rows]


def record_run(schedule_id: int, instruction: str, status: str, error: str | None = None) -> None:
    """The operator's own explicit requirement: log the FULL instruction
    text with every run, not just a reference to the schedule row, so
    what she was actually told to do stays recoverable even if the
    schedule is edited or deleted afterward."""
    store.write(lambda c: c.execute(
        "INSERT INTO schedule_runs(schedule_id, ts, instruction, status, error) VALUES (?,?,?,?,?)",
        (schedule_id, time.time(), instruction, status, str(error)[:1000] if error else None)))


def advance(schedule_id: int, schedule_type: str, interval_min: int | None, time_hour: int | None,
           time_minute: int | None, *, status: str, error: str | None, user_id: int,
           fired_ts: float | None = None) -> None:
    fired_ts = fired_ts if fired_ts is not None else time.time()
    next_run = _compute_next_run(schedule_type, interval_min, time_hour, time_minute, after=fired_ts,
                                 user_id=user_id)
    store.write(lambda c: c.execute(
        "UPDATE schedules SET last_run_ts=?, next_run_ts=?, last_status=?, last_error=? WHERE id=?",
        (fired_ts, next_run, status, str(error)[:500] if error else None, schedule_id)))


# ── tool registration ────────────────────────────────────────────────────
def _schedule_create_impl(session: dict, **kw) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return create(session, created_by_type=actor, created_by_peer_name=actor_peer_name, **kw)


def _schedule_edit_impl(session: dict, *, schedule_id: int, **kw) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return edit(session, schedule_id, actor=actor, actor_peer_name=actor_peer_name, **kw)


def _schedule_list_impl(session: dict) -> dict:
    return list_for_user(session)


def _schedule_delete_impl(session: dict, *, schedule_id: int) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return delete(session, schedule_id, actor=actor, actor_peer_name=actor_peer_name)


def _register_tools() -> None:
    import tools  # local: same reasoning as household.py/memory.py

    common_props = {
        "name": {"type": "string"},
        "instruction": {"type": "string",
                        "description": "Free text -- exactly what to act on when this fires. No fixed "
                                      "menu of task types; write it the way you'd tell a person what "
                                      "to do."},
        "required_tool": {"type": "string",
                          "description": "The exact tool name this task needs (e.g. 'ha_control', "
                                        "'web_search'), if any. Checked live right before firing -- "
                                        "if it isn't available or enabled by then, the task is skipped "
                                        "with a clear, logged reason instead of running and discovering "
                                        "it can't do the thing."},
        "deliver_to": {"type": "string", "enum": list(DELIVER_TO_VALUES),
                       "description": "'user' speaks to him normally when this fires. 'peer' tells a "
                                     "named peer instead (use that peer's own send tool) and stays "
                                     "quiet to him. 'both' does both. 'none' is act-don't-report -- do "
                                     "the thing, say nothing anywhere (message_user is still there for "
                                     "a genuine exception)."},
        "deliver_peer_id": {"type": "integer", "description": "Required when deliver_to is 'peer' or 'both'."},
        "schedule_type": {"type": "string", "enum": list(SCHEDULE_TYPES)},
        "interval_min": {"type": "integer",
                         "description": f"For schedule_type='interval': minutes between runs "
                                       f"({MIN_INTERVAL_MIN}-{MAX_INTERVAL_MIN})."},
        "time_hour": {"type": "integer", "description": "For schedule_type='time': 0-23, local time."},
        "time_minute": {"type": "integer", "description": "For schedule_type='time': 0-59."},
    }

    tools.register(tools.Tool(
        "schedule_create",
        {"type": "function", "function": {
            "name": "schedule_create",
            "description": "Create a scheduled task -- wakes you up later, on an interval or at a "
                           "specific time, with a free-text instruction to act on.",
            "parameters": {"type": "object", "properties": common_props,
                           "required": ["name", "instruction", "deliver_to", "schedule_type"]}}},
        _schedule_create_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "schedule_edit",
        {"type": "function", "function": {
            "name": "schedule_edit",
            "description": "Change one of your own existing scheduled tasks. Only pass the fields "
                           "you're actually changing.",
            "parameters": {"type": "object",
                           "properties": {"schedule_id": {"type": "integer"}, **common_props,
                                         "enabled": {"type": "boolean"}},
                           "required": ["schedule_id"]}}},
        _schedule_edit_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "schedule_list",
        {"type": "function", "function": {
            "name": "schedule_list",
            "description": "List your own scheduled tasks.",
            "parameters": {"type": "object", "properties": {}}}},
        _schedule_list_impl, min_role="member", data_scope="self", risk_tier="A"))

    tools.register(tools.Tool(
        "schedule_delete",
        {"type": "function", "function": {
            "name": "schedule_delete",
            "description": "Delete one of your own scheduled tasks permanently.",
            "parameters": {"type": "object", "properties": {"schedule_id": {"type": "integer"}},
                           "required": ["schedule_id"]}}},
        _schedule_delete_impl, min_role="member", data_scope="self", risk_tier="B"))


_register_tools()


def register_peer_actions() -> None:
    """Deferred to server.py's own main(), same reasoning as memory.py's
    and homeassistant.py's identical functions (see either one's own
    docstring for the exact circular-import failure this avoids).

    Both tools on the STANDARD trust ladder -- the operator's own explicit
    call: "peers at full trust get the same tools as Nori... [a sibling
    application] can create and edit schedules exactly as Nori can," no
    stricter
    full_trust_only tier for this feature, consistent with every other
    HA/settings tool this session built. schedule_list/schedule_delete
    aren't offered here -- listing/deleting HIS OWN account's schedules on
    a peer's say-so was never asked for, and 'prompt' trust already covers
    a peer's more mundane use of create/edit."""
    import peers
    peers.register_peer_requestable("schedule_create")
    peers.register_peer_requestable("schedule_edit")

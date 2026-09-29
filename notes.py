# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The board's second card type (2026-09-16) -- WISHLIST.md's original
card sketch (2026-09-11) named note/task/reminder/running-topic; tasks.py
built the "task" type first, this module is "note," the simplest of the
four: a title, a body, and a category, with none of a task's due date,
priority, recurrence, or close lifecycle. This module owns the `notes`/
`note_events` tables.

**Delete is real here, deliberately** -- the operator's own explicit
distinction from tasks: "note delete IS allowed here, unlike tasks
where it's close-only. That's deliberate on his part." There is no
close()/status column on a note at all; delete() actually removes the
row. note_events survives that anyway (no FK on note_id, same as every
other event table in this app) -- deleting a note doesn't erase the
record that it existed and what it said.

Category is the exact same fixed five tasks.py defined (personal/
household/work/business/other) -- imported directly from tasks.py
rather than redefined, so the two can never quietly drift apart.

**Two things the operator asked to be told rather than have assumed:**

- History: tasks.py has one; the operator didn't ask for one here.
  Built anyway -- the machinery (schedule_events' shape, already
  generalized once for tasks, see peers.actor_for()/_render_history() in
  server.py) is already fully reused with zero new code beyond this
  table, and "provenance" was explicitly asked for, which a bare
  created_by_type/created_by_peer_name column pair gives WHO created a
  note but not who changed it into what it now says after that --
  exactly the gap history exists to close. Easy to drop if he'd rather
  notes stay lighter-weight than this.
- Peers: wired in (2026-09-16, his own follow-up answer, once notes/
  tasks/reminders/trackers all existed) -- see register_peer_actions()
  below. note_delete's own peer path never actually deletes -- see
  delete()'s own docstring for the flag-instead-of-delete substitution.
"""
from __future__ import annotations

import time

import catalog
import store

CATEGORY_DOMAIN = "note"
# Was `= tasks.CATEGORIES` here (2026-09-16) -- widened the same day to
# catalog.py's own managed set, domain="note", its own independent
# (though identically-seeded) set from "task" -- the operator's own
# call, "tasks and notes may want to diverge," so each domain's rows
# are separate from the start even though they start out matching.

_TRACKED_FIELDS = ("title", "body", "category")

_UNSET = object()


def _validate(session: dict, *, title: str, category: str) -> str | None:
    if not (title or "").strip():
        return "title can't be empty"
    if not catalog.is_valid(session, CATEGORY_DOMAIN, category):
        return (f"{category!r} isn't a current note category -- check category_list, or "
               f"category_add it first")
    return None


def _log_event(note_id: int, actor: str, actor_peer_name: str | None, action: str,
               changes: dict | None = None, note: str | None = None) -> None:
    import json
    store.write(lambda c: c.execute(
        "INSERT INTO note_events(note_id, ts, actor, actor_peer_name, action, changes, note) "
        "VALUES (?,?,?,?,?,?,?)",
        (note_id, time.time(), actor, actor_peer_name, action,
         json.dumps(changes) if changes else None, note)))


def _diff(old: dict, new: dict) -> dict:
    changes = {}
    for f in _TRACKED_FIELDS:
        if old.get(f) != new.get(f):
            changes[f] = {"old": old.get(f), "new": new.get(f)}
    return changes


def add(session: dict, *, title: str, category: str, body: str | None = None,
       created_by_type: str = "user", created_by_peer_name: str | None = None) -> dict:
    # category has no default, same reasoning tasks.add() documents --
    # required in the tool schema is only ever advice to the model, not
    # something tools.dispatch() enforces; a real Python-required keyword
    # is what actually stops a silent 'other'.
    err = _validate(session, title=title, category=category)
    if err:
        return {"error": err}
    now = time.time()
    def _w(c):
        return c.execute(
            "INSERT INTO notes(user_id, title, body, category, created_by_type, created_by_peer_name, "
            "created_ts) VALUES (?,?,?,?,?,?,?)",
            (session["user_id"], title.strip(), body.strip() if body else None, category,
             created_by_type, created_by_peer_name, now)).lastrowid
    nid = store.write(_w)
    _log_event(nid, created_by_type, created_by_peer_name, "created")
    return {"ok": True, "note_id": nid}


def _get_owned(session: dict, note_id: int) -> dict | None:
    row = store.read(lambda c: c.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone())
    if row is None or row["user_id"] != session["user_id"]:
        return None
    return dict(row)


def update(session: dict, note_id: int, *, title=_UNSET, body=_UNSET, category=_UNSET,
          actor: str = "user", actor_peer_name: str | None = None) -> dict:
    row = _get_owned(session, note_id)
    if row is None:
        return {"error": "no such note"}
    merged = dict(row)
    for key, val in (("title", title), ("body", body), ("category", category)):
        if val is not _UNSET:
            merged[key] = val
    err = _validate(session, title=merged["title"], category=merged["category"])
    if err:
        return {"error": err}
    final = {**merged, "title": merged["title"].strip(),
            "body": (merged["body"].strip() if merged.get("body") else None)}
    store.write(lambda c: c.execute(
        "UPDATE notes SET title=?, body=?, category=? WHERE id=?",
        (final["title"], final["body"], final["category"], note_id)))
    changes = _diff(row, final)
    if changes:
        _log_event(note_id, actor, actor_peer_name, "updated", changes=changes)
    return {"ok": True}


def delete(session: dict, note_id: int, *, actor: str = "user", actor_peer_name: str | None = None) -> dict:
    """Real delete -- the operator's own explicit distinction from
    tasks.close(). Logged BEFORE the delete, same as every other
    delete-with-history in this app -- note_events has no FK on note_id,
    so the record of what this note said and who removed it survives
    the row itself being gone.

    **Peer-triggered delete never deletes outright** (2026-09-16, his own
    instruction: apply the same destructive-op handling memory.py's
    forget() established consistently here, "note delete is the obvious
    case") -- it flags instead, exactly memory.forget()'s own
    flag_removal substitution: trust still gates whether the request is
    honored at all (a 'none'/'prompt'/refused request never reaches this
    function), it just never needs a stricter full_trust_only tier on
    note_delete's own peer registration, because nothing irreversible
    can happen at any trust level. Reviewed on the notes settings tab
    via removal_candidates()/resolve_removal_flag() below, same
    "most-recent-event-wins" shape as memory's own review queue. Her own
    direct delete(), in a live conversation with him, is unchanged."""
    row = _get_owned(session, note_id)
    if row is None:
        return {"error": "no such note"}
    if actor == "peer":
        _log_event(note_id, actor, actor_peer_name, "flagged_removal",
                  note="a connected peer asked to delete this")
        return {"ok": True, "flagged": True,
               "note": "flagged for review on the notes settings tab rather than deleted "
                       "outright -- a peer-requested delete doesn't delete immediately"}
    _log_event(note_id, actor, actor_peer_name, "deleted", note=f"deleted \"{row['title']}\"")
    store.write(lambda c: c.execute("DELETE FROM notes WHERE id=?", (note_id,)))
    return {"ok": True}


def removal_candidates(session: dict, limit: int = 50) -> list[dict]:
    """Flags still awaiting his review -- a flag only shows up here if
    it's still the MOST RECENT event for that note, same "audit trail
    already carries this" shape as memory.removal_candidates() (any
    later touch -- an update, a dismiss, the note being deleted for real
    -- counts as handled without a dedicated 'resolved' column)."""
    rows = store.read(lambda c: c.execute(
        "SELECT e.* FROM note_events e JOIN notes n ON n.id = e.note_id "
        "WHERE n.user_id=? AND e.action='flagged_removal' "
        "AND e.ts = (SELECT MAX(ts) FROM note_events e2 WHERE e2.note_id = e.note_id) "
        "ORDER BY e.ts DESC LIMIT ?", (session["user_id"], limit)).fetchall())
    import json
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["changes"] = json.loads(d["changes"]) if d.get("changes") else {}
        except (ValueError, TypeError):
            d["changes"] = {}
        note_row = _get_owned(session, d["note_id"])
        d["note_title"] = note_row["title"] if note_row else "(already gone)"
        out.append(d)
    return out


def resolve_removal_flag(session: dict, note_id: int, action: str) -> dict:
    """The human half of "flag, don't act" -- called only from the
    settings-page review action, same shape as memory.resolve_removal_
    flag(). action='remove' deletes the row for real (same effect as his
    own note_delete would have had, just attributed to him); 'dismiss'
    logs a no-op event so the flag stops reappearing without touching
    the note."""
    row = _get_owned(session, note_id)
    if row is None:
        return {"error": "no such note"}
    if action == "remove":
        _log_event(note_id, "user", None, "deleted",
                  note=f"deleted \"{row['title']}\" (confirming a peer's flagged request)")
        store.write(lambda c: c.execute("DELETE FROM notes WHERE id=?", (note_id,)))
    elif action == "dismiss":
        _log_event(note_id, "user", None, "flag_dismissed", note="kept -- flag dismissed")
    else:
        return {"error": "action must be 'remove' or 'dismiss'"}
    return {"ok": True}


def set_needs_attention(session: dict, note_id: int, needs_attention: bool, *, actor: str = "user",
                        actor_peer_name: str | None = None) -> dict:
    """A new note starts needing his attention (schema default 1);
    opening it in the full-screen viewer modal is what actually clears
    it, not merely being rendered in the board's own stack (2026-09-17,
    his own design point: "everything marks read on page load" would
    make the feature do nothing). Nori (or a trusted peer) can set it
    either direction herself via this same function."""
    row = _get_owned(session, note_id)
    if row is None:
        return {"error": "no such note"}
    new_val = 1 if needs_attention else 0
    if row["needs_attention"] == new_val:
        return {"ok": True}
    store.write(lambda c: c.execute("UPDATE notes SET needs_attention=? WHERE id=?", (new_val, note_id)))
    _log_event(note_id, actor, actor_peer_name, "flagged",
              changes={"needs_attention": {"old": row["needs_attention"], "new": new_val}})
    return {"ok": True}


def list_for_user(session: dict, *, category: str | None = None) -> dict:
    # No catalog.is_valid() check here, deliberately -- same reasoning
    # tasks.list_for_user() documents: a filter by a disabled (or gone)
    # category name must still work, so he can still find what's filed
    # under it.
    if category is None:
        rows = store.read(lambda c: c.execute(
            "SELECT * FROM notes WHERE user_id=? ORDER BY created_ts DESC", (session["user_id"],)).fetchall())
    else:
        rows = store.read(lambda c: c.execute(
            "SELECT * FROM notes WHERE user_id=? AND category=? ORDER BY created_ts DESC",
            (session["user_id"], category)).fetchall())
    return {"notes": [dict(r) for r in rows]}


def get_for_user(session: dict, note_id: int) -> dict | None:
    return _get_owned(session, note_id)


def history_for(note_id: int, limit: int = 50) -> list[dict]:
    import json
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM note_events WHERE note_id=? ORDER BY ts DESC LIMIT ?",
        (note_id, limit)).fetchall())
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
def _note_add_impl(session: dict, **kw) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return add(session, created_by_type=actor, created_by_peer_name=actor_peer_name, **kw)


def _note_update_impl(session: dict, *, note_id: int, **kw) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return update(session, note_id, actor=actor, actor_peer_name=actor_peer_name, **kw)


def _note_delete_impl(session: dict, *, note_id: int) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return delete(session, note_id, actor=actor, actor_peer_name=actor_peer_name)


def _note_list_impl(session: dict, *, category: str | None = None) -> dict:
    return list_for_user(session, category=category)


def _note_set_needs_attention_impl(session: dict, *, note_id: int, needs_attention: bool) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return set_needs_attention(session, note_id, needs_attention, actor=actor, actor_peer_name=actor_peer_name)


def _register_tools() -> None:
    import tools  # local: same reasoning as household.py/memory.py/schedules.py/tasks.py

    tools.register(tools.Tool(
        "note_add",
        {"type": "function", "function": {
            "name": "note_add",
            "description": "Add a note to his board -- for something worth surfacing that isn't a "
                           "task (no due date, no completion state, just a title/body he can read "
                           "later).",
            "parameters": {"type": "object", "properties": {
                "title": {"type": "string"},
                "body": {"type": "string", "description": "Optional longer detail."},
                "category": {"type": "string",
                            "description": "Pick the one that actually fits -- don't default to "
                                           "'other' without thinking about it. Not a fixed list -- "
                                           "call category_list(domain='note') if unsure what "
                                           "currently exists, or category_add to create one."}},
                "required": ["title", "category"]}}},
        _note_add_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "note_update",
        {"type": "function", "function": {
            "name": "note_update",
            "description": "Change one of his existing notes -- only pass the fields you're "
                           "actually changing.",
            "parameters": {"type": "object", "properties": {
                "note_id": {"type": "integer"},
                "title": {"type": "string"}, "body": {"type": "string"},
                "category": {"type": "string"}},
                "required": ["note_id"]}}},
        _note_update_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "note_delete",
        {"type": "function", "function": {
            "name": "note_delete",
            "description": "Delete one of his notes permanently -- unlike a task, a note really is "
                           "removed, not just closed.",
            "parameters": {"type": "object", "properties": {"note_id": {"type": "integer"}},
                           "required": ["note_id"]}}},
        _note_delete_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "note_list",
        {"type": "function", "function": {
            "name": "note_list",
            "description": "List his notes -- so you know what's already on the board (and each "
                           "one's real id) before adding a duplicate or trying to update/delete one.",
            "parameters": {"type": "object", "properties": {
                "category": {"type": "string", "description": "Omit for every category."}}}}},
        _note_list_impl, min_role="member", data_scope="self", risk_tier="A"))

    tools.register(tools.Tool(
        "note_set_needs_attention",
        {"type": "function", "function": {
            "name": "note_set_needs_attention",
            "description": "Mark a note as needing his attention again, or as already seen. New "
                           "notes start needing attention automatically; this is for the exceptional "
                           "case -- e.g. re-flagging one, or clearing one he's clearly already read "
                           "based on what he says in conversation.",
            "parameters": {"type": "object", "properties": {
                "note_id": {"type": "integer"}, "needs_attention": {"type": "boolean"}},
                "required": ["note_id", "needs_attention"]}}},
        _note_set_needs_attention_impl, min_role="member", data_scope="self", risk_tier="B"))


_register_tools()


def register_peer_actions() -> None:
    """Deferred to server.py's own main(), same reasoning as memory.py's/
    homeassistant.py's/schedules.py's identical functions.

    All five tools on the STANDARD trust ladder, no full_trust_only tier
    -- his own instruction (2026-09-16): "peers get access to everything
    just built... gated on trust level, same pattern as the settings
    tool and MCP toggles," extended the same way for note_set_needs_
    attention (2026-09-17, his own follow-up: "peers can presumably set
    the flag too... follow the same trust gating"). note_delete needs no
    stricter tier of its own: delete()'s own peer path already never
    deletes outright (see its docstring), so trust still gates whether
    the request is honored at all, but nothing irreversible can happen
    at any tier regardless."""
    import peers
    peers.register_peer_requestable("note_add")
    peers.register_peer_requestable("note_update")
    peers.register_peer_requestable("note_delete")
    peers.register_peer_requestable("note_list")
    peers.register_peer_requestable("note_set_needs_attention")


# ── proactive-ping signal (2026-09-26) ──────────────────────────────────
_STALE_NOTE_HOURS = 24


def scheduler_signal(user_id: int) -> str | None:
    """A board note flagged needs_attention that's been sitting a while --
    conservative on purpose (the operator: "one bad threshold teaches him to
    ignore her"): notes have no due date of their own, so created_ts is
    used as a stand-in for "flagged since" (a note re-flagged after being
    cleared once would read as older than it really is -- a known,
    accepted approximation, not a precise flag-time). Never nags about a
    note the instant it's created; only one that's sat unattended."""
    cutoff = time.time() - _STALE_NOTE_HOURS * 3600
    rows = store.read(lambda c: c.execute(
        "SELECT title FROM notes WHERE user_id=? AND needs_attention=1 AND created_ts<? "
        "ORDER BY created_ts", (user_id, cutoff)).fetchall())
    if not rows:
        return None
    titles = ", ".join(r["title"] for r in rows[:5])
    if len(rows) > 5:
        titles += f", and {len(rows) - 5} more"
    return f"board notes waiting on you: {titles}"


import scheduler  # local-at-module-bottom on purpose: registers this module's signal once, at import
scheduler.register_signal(scheduler_signal, key="stale_notes", label="Board notes waiting on you",
                          tier="routine", cooldown_seconds=24 * 3600)

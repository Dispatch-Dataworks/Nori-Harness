# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Manageable named categories (2026-09-16) -- personal/household/work/
business/other started as a hardcoded 5-tuple on tasks.py (and reused by
notes.py); widened here the moment the operator asked for her AND him to
be able to create/rename/disable category sets themselves, not just
pick from a fixed list. `domain` scopes a name to one item type
(task/note/reminder) -- seeded identically today, but a domain COULD
diverge later without a schema change, since each domain's own rows are
independent from the start.

**Reuses the exact pattern tracker_types already established** (declared
name, disable-never-delete, provenance, actor-tagged history) -- the
operator's own instruction: "the same shape as tracker types... one
mechanism serving both." Deliberately its OWN table rather than
tracker_types itself made generic, though -- see store.py's own schema
comment for why a physical merge was evaluated and passed on (tracker_
types carries real structured fields -- value_kind/unit/value_meta -- a
bare category never needs, and merging them would relocate those into
an opaque JSON blob for a worse fit, not a better one, for essentially
zero benefit given there was no live tracker data yet to justify the
migration risk). The PATTERN is what's actually shared -- this is the
fifth independent table built on top of it (schedules/tasks/notes/
trackers were the first four), each with its own schema, none of them
literally sharing physical storage with another.

**No static tool-schema enum, deliberately** -- the operator's own
answer to his own question: "validating against the live set at
dispatch is the likely answer, since a schema enum baked at registration
time would go stale the moment she adds one." tasks.py/notes.py's own
category params are now a plain string, checked via is_valid() inside
their own _validate() at CALL time, never against a list frozen when
tools.py registered the schema.

**Existing items keep working if their category is disabled** -- by
construction: a task/note's own `category` column is just a stored
string, never a foreign key: disabling a categories row only ever
affects whether NEW items can be filed under it (is_valid() returns
False) and whether it's offered going forward; it never touches
anything already filed under that name.
"""
from __future__ import annotations

import time

import store

# Extend as a new item type grows its own categories (reminders, next).
DOMAINS = ("task", "note", "reminder")

# A task/note's own `category` column is a plain string, not a foreign
# key to this table (same reasoning tracker_entries' real FK to
# tracker_types does NOT apply here -- categories are meant to be
# renameable text, not an immutable id an item merely points at). That
# means a rename has to actually UPDATE every existing item's stored
# string too, or "rename" would only ever affect future items -- not
# what the word means. This is the one place that cascade is expressed;
# extend it the same way when reminders.py lands its own table.
_ITEM_TABLES = {"task": ("tasks", "category"), "note": ("notes", "category")}

# Seeded once per (user, domain) the first time anything touches that
# domain -- lazy, not a migration step, so it self-heals for a user
# created before this module existed AND for one created after, with no
# separate hook into account creation needed either way.
_DEFAULT_NAMES = ("personal", "household", "work", "business", "other")


def _ensure_seeded(user_id: int, domain: str) -> None:
    row = store.read(lambda c: c.execute(
        "SELECT 1 FROM categories WHERE user_id=? AND domain=? LIMIT 1", (user_id, domain)).fetchone())
    if row is not None:
        return
    now = time.time()
    def _w(c):
        for name in _DEFAULT_NAMES:
            c.execute(
                "INSERT INTO categories(user_id, domain, name, enabled, created_by_type, created_ts) "
                "VALUES (?,?,?,1,'user',?)", (user_id, domain, name, now))
    store.write(_w)


def _log_event(category_id: int, actor: str, actor_peer_name: str | None, action: str,
               changes: dict | None = None) -> None:
    import json
    store.write(lambda c: c.execute(
        "INSERT INTO category_events(category_id, ts, actor, actor_peer_name, action, changes) "
        "VALUES (?,?,?,?,?,?)",
        (category_id, time.time(), actor, actor_peer_name, action, json.dumps(changes) if changes else None)))


def is_valid(session: dict, domain: str, name: str) -> bool:
    """The dispatch-time check tasks.py/notes.py call instead of a
    static enum -- see the module docstring."""
    _ensure_seeded(session["user_id"], domain)
    row = store.read(lambda c: c.execute(
        "SELECT 1 FROM categories WHERE user_id=? AND domain=? AND name=? AND enabled=1",
        (session["user_id"], domain, name)).fetchone())
    return row is not None


def list_categories(session: dict, domain: str, *, enabled_only: bool = False) -> dict:
    if domain not in DOMAINS:
        return {"error": f"domain must be one of: {', '.join(DOMAINS)}"}
    _ensure_seeded(session["user_id"], domain)
    if enabled_only:
        rows = store.read(lambda c: c.execute(
            "SELECT * FROM categories WHERE user_id=? AND domain=? AND enabled=1 ORDER BY name",
            (session["user_id"], domain)).fetchall())
    else:
        rows = store.read(lambda c: c.execute(
            "SELECT * FROM categories WHERE user_id=? AND domain=? ORDER BY name",
            (session["user_id"], domain)).fetchall())
    return {"categories": [dict(r) for r in rows]}


def _get_owned(session: dict, category_id: int) -> dict | None:
    row = store.read(lambda c: c.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone())
    if row is None or row["user_id"] != session["user_id"]:
        return None
    return dict(row)


def add(session: dict, *, domain: str, name: str, created_by_type: str = "user",
       created_by_peer_name: str | None = None) -> dict:
    if domain not in DOMAINS:
        return {"error": f"domain must be one of: {', '.join(DOMAINS)}"}
    name = (name or "").strip()
    if not name:
        return {"error": "name can't be empty"}
    _ensure_seeded(session["user_id"], domain)
    existing = store.read(lambda c: c.execute(
        "SELECT 1 FROM categories WHERE user_id=? AND domain=? AND name=?",
        (session["user_id"], domain, name)).fetchone())
    if existing:
        return {"error": f"a {domain} category named {name!r} already exists"}
    now = time.time()
    def _w(c):
        return c.execute(
            "INSERT INTO categories(user_id, domain, name, enabled, created_by_type, created_by_peer_name, "
            "created_ts) VALUES (?,?,?,1,?,?,?)",
            (session["user_id"], domain, name, created_by_type, created_by_peer_name, now)).lastrowid
    cid = store.write(_w)
    _log_event(cid, created_by_type, created_by_peer_name, "created")
    return {"ok": True, "category_id": cid}


def rename(session: dict, category_id: int, *, new_name: str, actor: str = "user",
          actor_peer_name: str | None = None) -> dict:
    row = _get_owned(session, category_id)
    if row is None:
        return {"error": "no such category"}
    new_name = (new_name or "").strip()
    if not new_name:
        return {"error": "name can't be empty"}
    dup = store.read(lambda c: c.execute(
        "SELECT 1 FROM categories WHERE user_id=? AND domain=? AND name=? AND id!=?",
        (session["user_id"], row["domain"], new_name, category_id)).fetchone())
    if dup:
        return {"error": f"a {row['domain']} category named {new_name!r} already exists"}
    if new_name == row["name"]:
        return {"ok": True}  # no-op -- nothing to log
    old_name = row["name"]
    store.write(lambda c: c.execute("UPDATE categories SET name=? WHERE id=?", (new_name, category_id)))
    table = _ITEM_TABLES.get(row["domain"])
    if table:
        tbl, col = table
        store.write(lambda c: c.execute(
            f"UPDATE {tbl} SET {col}=? WHERE user_id=? AND {col}=?",
            (new_name, session["user_id"], old_name)))
    _log_event(category_id, actor, actor_peer_name, "renamed", changes={"name": {"old": old_name, "new": new_name}})
    return {"ok": True}


def set_enabled(session: dict, category_id: int, enabled: bool, *, actor: str = "user",
                actor_peer_name: str | None = None) -> dict:
    row = _get_owned(session, category_id)
    if row is None:
        return {"error": "no such category"}
    new_val = 1 if enabled else 0
    if row["enabled"] == new_val:
        return {"ok": True}
    store.write(lambda c: c.execute("UPDATE categories SET enabled=? WHERE id=?", (new_val, category_id)))
    _log_event(category_id, actor, actor_peer_name, "enabled" if enabled else "disabled")
    return {"ok": True}


def history_for(category_id: int, limit: int = 50) -> list[dict]:
    import json
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM category_events WHERE category_id=? ORDER BY ts DESC LIMIT ?",
        (category_id, limit)).fetchall())
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
def _category_add_impl(session: dict, **kw) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return add(session, created_by_type=actor, created_by_peer_name=actor_peer_name, **kw)


def _category_rename_impl(session: dict, *, category_id: int, **kw) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return rename(session, category_id, actor=actor, actor_peer_name=actor_peer_name, **kw)


def _category_disable_impl(session: dict, *, category_id: int) -> dict:
    import peers
    actor, actor_peer_name = peers.actor_for(session)
    return set_enabled(session, category_id, False, actor=actor, actor_peer_name=actor_peer_name)


def _category_list_impl(session: dict, *, domain: str, enabled_only: bool = False) -> dict:
    return list_categories(session, domain, enabled_only=enabled_only)


def _register_tools() -> None:
    import tools  # local: same reasoning as every other subsystem module in this app

    tools.register(tools.Tool(
        "category_add",
        {"type": "function", "function": {
            "name": "category_add",
            "description": "Add a new category for tasks, notes, or reminders -- these aren't a "
                           "fixed list, you can extend the set yourself.",
            "parameters": {"type": "object", "properties": {
                "domain": {"type": "string", "enum": list(DOMAINS)},
                "name": {"type": "string"}},
                "required": ["domain", "name"]}}},
        _category_add_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "category_rename",
        {"type": "function", "function": {
            "name": "category_rename",
            "description": "Rename one of his existing categories.",
            "parameters": {"type": "object", "properties": {
                "category_id": {"type": "integer"}, "new_name": {"type": "string"}},
                "required": ["category_id", "new_name"]}}},
        _category_rename_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "category_disable",
        {"type": "function", "function": {
            "name": "category_disable",
            "description": "Disable a category -- never deletes it. Anything already filed under "
                           "it keeps that category; this only stops it being offered for new items.",
            "parameters": {"type": "object", "properties": {"category_id": {"type": "integer"}},
                           "required": ["category_id"]}}},
        _category_disable_impl, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "category_list",
        {"type": "function", "function": {
            "name": "category_list",
            "description": "List the current categories for tasks, notes, or reminders -- call this "
                           "before filing something if you're not sure what already exists (the set "
                           "isn't fixed, and started as personal/household/work/business/other but "
                           "may have changed).",
            "parameters": {"type": "object", "properties": {
                "domain": {"type": "string", "enum": list(DOMAINS)},
                "enabled_only": {"type": "boolean"}},
                "required": ["domain"]}}},
        _category_list_impl, min_role="member", data_scope="self", risk_tier="A"))


_register_tools()

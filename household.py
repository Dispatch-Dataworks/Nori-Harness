# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Household inventory -- the first real purpose tool, and the first real
consumer of workspace-scoped shared writes (deliberately deferred back in
Phase 5 until a concrete feature actually needed the RBAC decision: any
household member should be able to say "we're out of milk," so this is
member-callable despite touching shared workspace data, not gated to
admin the way a Tier C default would be -- a household grocery list is
mundane, not consequential, and gatekeeping it would defeat the point of
a shared tool).

The only module with raw SQL against `household_items`.
"""
from __future__ import annotations

import time

import store

STATUSES = ("ok", "low", "out")


def upsert_item(session: dict, name: str, status: str = "ok", quantity: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        return {"error": "item name can't be empty"}
    if status not in STATUSES:
        return {"error": f"status must be one of: {', '.join(STATUSES)}"}
    now = time.time()
    wid = session["workspace_id"]
    def _w(c):
        row = c.execute("SELECT id FROM household_items WHERE workspace_id=? AND name=?",
                        (wid, name)).fetchone()
        if row:
            c.execute("UPDATE household_items SET status=?, quantity=?, updated_ts=?, updated_by=? "
                     "WHERE id=?", (status, quantity, now, session["user_id"], row["id"]))
            return row["id"]
        return c.execute(
            "INSERT INTO household_items(workspace_id, name, quantity, status, updated_ts, updated_by) "
            "VALUES (?,?,?,?,?,?)", (wid, name, quantity, status, now, session["user_id"])).lastrowid
    item_id = store.write(_w)
    return {"ok": True, "item_id": item_id, "name": name, "status": status}


def list_items(session: dict, status: str | None = None, include_meta: bool = False) -> dict:
    """include_meta pulls updated_ts/updated_by too -- off by default so
    Nori's own list_household_items tool (whose schema never offers this
    kwarg) keeps seeing the same plain shape it always has; the household
    UI page passes include_meta=True to show "who/when" per item without
    a second, parallel query against this table."""
    wid = session["workspace_id"]
    cols = "name, quantity, status" + (", updated_ts, updated_by" if include_meta else "")
    if status:
        if status not in STATUSES:
            return {"error": f"status must be one of: {', '.join(STATUSES)}"}
        rows = store.read(lambda c: c.execute(
            f"SELECT {cols} FROM household_items WHERE workspace_id=? AND status=? "
            "ORDER BY name", (wid, status)).fetchall())
    else:
        rows = store.read(lambda c: c.execute(
            f"SELECT {cols} FROM household_items WHERE workspace_id=? ORDER BY name",
            (wid,)).fetchall())
    return {"items": [dict(r) for r in rows]}


def remove_item(session: dict, name: str) -> dict:
    wid = session["workspace_id"]
    row = store.read(lambda c: c.execute(
        "SELECT id FROM household_items WHERE workspace_id=? AND name=?", (wid, name)).fetchone())
    if row is None:
        return {"error": "no such item"}
    store.write(lambda c: c.execute("DELETE FROM household_items WHERE id=?", (row["id"],)))
    return {"ok": True}


def scheduler_signal(user_id: int) -> str | None:
    """Registered with scheduler.py -- the first real signal it ever gets,
    closing the loop Phase 9 built with nothing yet to react to."""
    row = store.read(lambda c: c.execute("SELECT workspace_id FROM users WHERE id=?", (user_id,)).fetchone())
    if row is None:
        return None
    low = store.read(lambda c: c.execute(
        "SELECT name FROM household_items WHERE workspace_id=? AND status IN ('low','out') ORDER BY name",
        (row["workspace_id"],)).fetchall())
    if not low:
        return None
    names = ", ".join(r["name"] for r in low)
    return f"the household inventory is low or out on: {names}"


# ── tool registration ────────────────────────────────────────────────────
def _register_tools() -> None:
    import tools  # local: same reasoning as memory.py/emotion.py

    tools.register(tools.Tool(
        "update_household_item",
        {"type": "function", "function": {
            "name": "update_household_item",
            "description": "Add or update a shared household inventory item -- visible to everyone in the household.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string"},
                "status": {"type": "string", "enum": list(STATUSES)},
                "quantity": {"type": "string", "description": "free text, e.g. '2' or 'half a box' -- optional"}},
                "required": ["name", "status"]}}},
        lambda session, **kw: upsert_item(session, **kw), min_role="member", data_scope="workspace", risk_tier="B"))

    tools.register(tools.Tool(
        "list_household_items",
        {"type": "function", "function": {
            "name": "list_household_items",
            "description": "List the shared household inventory, optionally filtered by status.",
            "parameters": {"type": "object", "properties": {
                "status": {"type": "string", "enum": list(STATUSES)}}}}},
        lambda session, **kw: list_items(session, **kw), min_role="member", data_scope="workspace", risk_tier="A"))

    tools.register(tools.Tool(
        "remove_household_item",
        {"type": "function", "function": {
            "name": "remove_household_item",
            "description": "Remove an item from the shared household inventory entirely (not just mark it ok).",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string"}}, "required": ["name"]}}},
        lambda session, **kw: remove_item(session, **kw), min_role="member", data_scope="workspace", risk_tier="B"))


_register_tools()

import scheduler  # local-at-module-bottom on purpose: registers this module's signal once, at import
scheduler.register_signal(scheduler_signal, key="household_inventory", label="Household inventory running low or out")

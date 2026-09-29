# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Meal planning -- the second self-contained purpose tool, same shape as
household.py: workspace-scoped, member-callable despite touching shared
data, since planning dinner is exactly the kind of mundane shared task
that shouldn't need admin gatekeeping.

The only module with raw SQL against `meal_plan`.
"""
from __future__ import annotations

import datetime
import time

import store

MEAL_TYPES = ("breakfast", "lunch", "dinner")


def _valid_date(s: str) -> bool:
    try:
        datetime.date.fromisoformat(s)
        return True
    except (ValueError, TypeError):
        return False


def set_meal(session: dict, meal_date: str, description: str, meal_type: str = "dinner") -> dict:
    if not _valid_date(meal_date):
        return {"error": "meal_date must be 'YYYY-MM-DD'"}
    if meal_type not in MEAL_TYPES:
        return {"error": f"meal_type must be one of: {', '.join(MEAL_TYPES)}"}
    description = (description or "").strip()
    if not description:
        return {"error": "description can't be empty"}
    now = time.time()
    wid = session["workspace_id"]
    def _w(c):
        row = c.execute(
            "SELECT id FROM meal_plan WHERE workspace_id=? AND meal_date=? AND meal_type=?",
            (wid, meal_date, meal_type)).fetchone()
        if row:
            c.execute("UPDATE meal_plan SET description=?, updated_ts=?, updated_by=? WHERE id=?",
                     (description, now, session["user_id"], row["id"]))
            return row["id"]
        return c.execute(
            "INSERT INTO meal_plan(workspace_id, meal_date, meal_type, description, updated_ts, updated_by) "
            "VALUES (?,?,?,?,?,?)",
            (wid, meal_date, meal_type, description, now, session["user_id"])).lastrowid
    mid = store.write(_w)
    return {"ok": True, "meal_id": mid, "meal_date": meal_date, "meal_type": meal_type}


def list_meals(session: dict, from_date: str | None = None, to_date: str | None = None) -> dict:
    wid = session["workspace_id"]
    clauses = ["workspace_id=?"]
    params: list = [wid]
    if from_date:
        if not _valid_date(from_date):
            return {"error": "from_date must be 'YYYY-MM-DD'"}
        clauses.append("meal_date >= ?")
        params.append(from_date)
    if to_date:
        if not _valid_date(to_date):
            return {"error": "to_date must be 'YYYY-MM-DD'"}
        clauses.append("meal_date <= ?")
        params.append(to_date)
    rows = store.read(lambda c: c.execute(
        f"SELECT meal_date, meal_type, description FROM meal_plan WHERE {' AND '.join(clauses)} "
        "ORDER BY meal_date, meal_type", params).fetchall())
    return {"meals": [dict(r) for r in rows]}


def clear_meal(session: dict, meal_date: str, meal_type: str = "dinner") -> dict:
    wid = session["workspace_id"]
    store.write(lambda c: c.execute(
        "DELETE FROM meal_plan WHERE workspace_id=? AND meal_date=? AND meal_type=?",
        (wid, meal_date, meal_type)))
    return {"ok": True}


def scheduler_signal(user_id: int) -> str | None:
    """No dinner planned for tonight -- the second real scheduler signal."""
    row = store.read(lambda c: c.execute("SELECT workspace_id FROM users WHERE id=?", (user_id,)).fetchone())
    if row is None:
        return None
    today = datetime.date.today().isoformat()
    existing = store.read(lambda c: c.execute(
        "SELECT 1 FROM meal_plan WHERE workspace_id=? AND meal_date=? AND meal_type='dinner'",
        (row["workspace_id"], today)).fetchone())
    if existing:
        return None
    return "there's no dinner planned for tonight yet"


# ── tool registration ────────────────────────────────────────────────────
def _register_tools() -> None:
    import tools  # local: same reasoning as memory.py/emotion.py

    tools.register(tools.Tool(
        "plan_meal",
        {"type": "function", "function": {
            "name": "plan_meal",
            "description": "Set or update what's planned for a meal -- shared across the household.",
            "parameters": {"type": "object", "properties": {
                "meal_date": {"type": "string", "description": "YYYY-MM-DD"},
                "meal_type": {"type": "string", "enum": list(MEAL_TYPES)},
                "description": {"type": "string"}},
                "required": ["meal_date", "description"]}}},
        lambda session, **kw: set_meal(session, **kw), min_role="member", data_scope="workspace", risk_tier="B"))

    tools.register(tools.Tool(
        "list_meal_plan",
        {"type": "function", "function": {
            "name": "list_meal_plan",
            "description": "List the shared meal plan, optionally within a date range.",
            "parameters": {"type": "object", "properties": {
                "from_date": {"type": "string", "description": "YYYY-MM-DD, optional"},
                "to_date": {"type": "string", "description": "YYYY-MM-DD, optional"}}}}},
        lambda session, **kw: list_meals(session, **kw), min_role="member", data_scope="workspace", risk_tier="A"))

    tools.register(tools.Tool(
        "clear_meal_plan",
        {"type": "function", "function": {
            "name": "clear_meal_plan",
            "description": "Clear a planned meal.",
            "parameters": {"type": "object", "properties": {
                "meal_date": {"type": "string", "description": "YYYY-MM-DD"},
                "meal_type": {"type": "string", "enum": list(MEAL_TYPES)}},
                "required": ["meal_date"]}}},
        lambda session, **kw: clear_meal(session, **kw), min_role="member", data_scope="workspace", risk_tier="B"))


_register_tools()

import scheduler  # local-at-module-bottom on purpose: registers this module's signal once, at import
scheduler.register_signal(scheduler_signal, key="meal_planning", label="No dinner planned for tonight")

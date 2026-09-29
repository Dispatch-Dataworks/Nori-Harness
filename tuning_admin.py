# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Context tuning, as three layers: the SHIPPED DEFAULT (config.py's own defaults, never changed by any action), your BASELINE (a last-known-good you marked on purpose),
and the CURRENT values (freely edited). The same shape as the persona editor, and independent of it: nothing here touches the persona and nothing there touches these.

  * The baseline is set by one deliberate action ("save the current values as my baseline") and by nothing else. Recording it automatically after each change would make it
    drift into whatever was typed last, which is the thing it exists to prevent. Its date is stored with it.
  * It lives in its own table (`tuning_baseline`), not in `settings`. Every action here that changes values goes through config.set on the nine tuning keys and nowhere else, so
    a reset, a restore, an edit or a migration of settings cannot reach it, and a trigger refuses to delete it. Only "save as baseline" replaces it.
  * Every change first records the values it replaces (`tuning_history`, append-only), so "back one edit" always has somewhere to go and a restore never loses what it replaced.
  * A restore is validated in full before anything is written: all values or none.

Operator only: reached only from server.py's admin-gated routes behind the CSRF check; no tool imports it (tests/test_nori_tuning_admin.py checks that structurally).
"""
from __future__ import annotations

import json
import time

import config
import restore_ui
import store

KEYS = ("context_window_msgs", "compaction_enabled", "compaction_max_segments", "compaction_budget_tokens", "compaction_session_gap_hours", "peer_recent_cap",
        "peer_recent_window_hours", "memory_max_tokens", "memory_pinned_max_tokens")
ACTIONS = ("baseline_save", "baseline_revert", "reset_default", "previous", "restore")
HISTORY_SHOWN = 20


def current(ws: int) -> dict:
    return {k: config.get("workspace", ws, k) for k in KEYS}


def defaults() -> dict:
    return {k: config.spec()[k][0] for k in KEYS}


def _validate(values: dict) -> tuple[dict | None, str | None]:
    """The whole set coerced and bounds-checked, or a reason nothing may be written. Missing keys are refused: a restore is always of a complete set."""
    out = {}
    for k in KEYS:
        if k not in values:
            return None, f"{k} is missing"
        try:
            v = config._coerce(values[k], config.spec()[k][1])
        except (ValueError, TypeError):
            return None, f"{k} is not a valid value"
        b = config._CONTEXT_BOUNDS.get(k)
        if b and not b[0] <= v <= b[1]:
            return None, f"{k} must be between {b[0]} and {b[1]}"
        out[k] = v
    return out, None


def _record(ws: int, values: dict, action: str) -> None:
    store.write(lambda c: c.execute("INSERT INTO tuning_history(workspace_id, ts, values_json, action) VALUES (?,?,?,?)", (ws, time.time(), json.dumps(values, sort_keys=True), action)))


def history(ws: int, limit: int = HISTORY_SHOWN) -> list[dict]:
    rows = store.read(lambda c: c.execute("SELECT id, ts, values_json, action FROM tuning_history WHERE workspace_id=? ORDER BY id DESC LIMIT ?", (ws, limit)).fetchall())
    return [{"id": r["id"], "ts": r["ts"], "action": r["action"], "values": json.loads(r["values_json"])} for r in rows]


def history_row(ws: int, hid) -> dict | None:
    try:
        hid = int(hid)
    except (TypeError, ValueError):
        return None
    r = store.read(lambda c: c.execute("SELECT id, ts, values_json, action FROM tuning_history WHERE workspace_id=? AND id=?", (ws, hid)).fetchone())
    return {"id": r["id"], "ts": r["ts"], "action": r["action"], "values": json.loads(r["values_json"])} if r else None


def previous(ws: int) -> dict | None:
    """The most recent earlier state that differs from the live values: what "back one edit" restores."""
    cur = current(ws)
    for h in history(ws, 200):
        if h["values"] != cur:
            return h
    return None


def baseline(ws: int) -> dict | None:
    r = store.read(lambda c: c.execute("SELECT values_json, saved_ts FROM tuning_baseline WHERE workspace_id=?", (ws,)).fetchone())
    return {"values": json.loads(r["values_json"]), "saved_ts": r["saved_ts"]} if r else None


def _apply(ws: int, values: dict, action: str) -> tuple[bool, str]:
    new, err = _validate(values)
    if err:
        return False, f"nothing changed: {err}"
    cur = current(ws)
    if cur == new:
        return True, "nothing to change: these values are already live"
    _record(ws, cur, action)                       # what is being replaced, kept before anything is written
    for k in KEYS:
        if cur[k] != new[k]:
            config.set("workspace", ws, k, new[k])
    return True, "applied"


def target_values(ws: int, target: str, hid=None) -> tuple[dict | None, str]:
    """(values, human label) for a restore target: 'baseline', 'default', 'previous' or 'history' (with an id)."""
    if target == "baseline":
        b = baseline(ws)
        return (b["values"], "your saved baseline") if b else (None, "no baseline saved yet")
    if target == "default":
        return defaults(), "the shipped defaults"
    if target == "previous":
        h = previous(ws)
        return (h["values"], "the values before your last change") if h else (None, "no earlier values to go back to")
    if target == "history":
        h = history_row(ws, hid)
        return (h["values"], "an earlier version") if h else (None, "no such earlier version")
    return None, "unknown target"


def preview_rows(ws: int, target: str, hid=None) -> list[dict] | None:
    vals, _ = target_values(ws, target, hid)
    if vals is None:
        return None
    cur = current(ws)
    spec = config.spec()
    return [{"key": k, "desc": spec[k][4] if len(spec[k]) > 4 else k, "current": cur[k], "target": vals[k], "changed": cur[k] != vals[k]} for k in KEYS]


def act(ws: int, user_id: int | None, action: str, form: dict) -> tuple[bool, str]:
    """One action. Returns (ok, message); a refused action changes nothing. `save` takes `values` (a dict of the keys being changed; the rest keep their current value)."""
    if action == "save":
        vals = form.get("values")
        if not isinstance(vals, dict):
            return False, "no values to save"
        merged = {**current(ws), **{k: v for k, v in vals.items() if k in KEYS}}
        ok, msg = _apply(ws, merged, "save")
        return ok, ("saved" if ok and msg == "applied" else msg)
    if action == "baseline_save":
        cur = current(ws)
        store.write(lambda c: c.execute(
            "INSERT INTO tuning_baseline(workspace_id, values_json, saved_ts, saved_by) VALUES (?,?,?,?) "
            "ON CONFLICT(workspace_id) DO UPDATE SET values_json=excluded.values_json, saved_ts=excluded.saved_ts, saved_by=excluded.saved_by",
            (ws, json.dumps(cur, sort_keys=True), time.time(), user_id)))
        return True, "saved the current values as your baseline"
    if action in ("baseline_revert", "reset_default", "previous", "restore"):
        target = {"baseline_revert": "baseline", "reset_default": "default", "previous": "previous", "restore": "history"}[action]
        vals, label = target_values(ws, target, form.get("id"))
        if vals is None:
            return False, f"not restored: {label}"
        ok, msg = _apply(ws, vals, action)
        return ok, (f"restored {label} (the values it replaced are in the history)" if ok and msg == "applied" else msg)
    return False, "unknown action"


# ---------------------------------------------------------------- the page pieces
_RESTORE = {"baseline": ("baseline_revert", "Restore my baseline"), "default": ("reset_default", "Restore the shipped defaults"),
            "previous": ("previous", "Go back one edit"), "history": ("restore", "Restore these values")}


def render_preview(ws: int, target: str, hid, csrf: str) -> str | None:
    """What a restore would change, before it does anything. None when the target does not exist."""
    rows = preview_rows(ws, target, hid)
    if rows is None:
        return None
    _vals, label = target_values(ws, target, hid)
    action, button = _RESTORE[target]
    return restore_ui.preview_page(
        heading=f"Restore {label}", restores_to=f"This changes the context-tuning values to {label}. It does not touch the persona. The values it replaces are kept in the history.",
        body_html=restore_ui.rows_html(rows), csrf=csrf, action_path=f"/admin/contexttuning/{action}", fields={"id": hid} if target == "history" else {},
        back_href="/settings?tab=contexttuning", confirm=f"Change the tuning values to {label}?", button=button)


def render_layers(ws: int, csrf: str) -> str:
    """The going-back section of the Context tuning page: three targets plus the deliberate 'save as baseline'."""
    b = baseline(ws)
    prev = previous(ws)
    hist = history(ws)
    cards = (
        restore_ui.card("Back to my baseline", "The values you last marked as good, on purpose. Only saving a new baseline changes it: editing, resetting or restoring never touches it.",
                        "/admin/contexttuning/preview?target=baseline" if b else None,
                        note=(f"Saved {restore_ui.when(b['saved_ts'])} ({restore_ui.age(b['saved_ts'])})." if b else "You have not saved one yet: use the button below when you like how she is running.")) +
        restore_ui.card("Back to the shipped defaults", "What a fresh install runs with. They never change and are always available.", "/admin/contexttuning/preview?target=default") +
        restore_ui.card("Back one edit", "The values before your last change. Doing it twice puts you back where you started; to go further back, pick one from the history below.",
                        "/admin/contexttuning/preview?target=previous" if prev else None,
                        note=(f"From {restore_ui.when(prev['ts'])}." if prev else "")))
    confirm = (f"Replace your baseline from {restore_ui.when(b['saved_ts'])} with the values that are live now?" if b else "Save the values that are live now as your baseline?")
    rows = "".join(f"<tr><td>{restore_ui.when(h['ts'])}</td><td class=muted>{restore_ui.esc(h['action'])}</td>"
                   f"<td><a href='/admin/contexttuning/preview?target=history&id={h['id']}'>preview and restore</a></td></tr>" for h in hist)         or "<tr><td class=muted>No earlier values yet: your first change will be kept here.</td></tr>"
    return ("<div class=section><h2>going back</h2>"
            "<p class=muted>Three layers: the <b>shipped defaults</b> (never change), your <b>baseline</b> (a last-known-good you set on purpose) and the <b>live values</b> above. "
            "Each way back shows exactly what would change before it does anything. This is separate from the persona: restoring one never touches the other.</p>"
            f"{cards}<form method=post action='/admin/contexttuning/baseline_save' data-confirm='{restore_ui.esc(confirm)}' style='margin-top:.6rem'>"
            f"<input type=hidden name=csrf value='{restore_ui.esc(csrf)}'><button class='btn btn-primary'>save the current values as my baseline</button></form></div>"
            f"<div class=section><h2>history</h2><div class=table-scroll><table>{rows}</table></div></div>")

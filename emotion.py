# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Nori's visible emotional state, per user -- the only module that runs
raw SQL against `emotion_state` (see store.py's narrow-abstraction rule).

The valid state list is config, not code (emotions.json) -- adding a state
means adding an entry + an avatar file, not touching this module. That
matters for shareability too: another operator can ship a different set
entirely without a code change.

Design, per the brief this was built from:
  - exposed as a tool (set_emotion), enum-constrained to the configured
    list so the model can't invent a state
  - persists rather than resetting -- a turn with no set_emotion call
    just keeps the current state
  - decays to neutral after a period of real wall-clock inactivity
    (NORI_EMOTION_DECAY_MINUTES, default 45) -- computed at READ time from
    (state, updated_ts), never written back by a background process, same
    "derived, not stored" pattern a sibling application uses for overdue tasks. A
    turn-count-based decay was considered and rejected: these 20 states
    aren't on one ordered scale, so "N turns of silence" doesn't mean the
    same thing after a 30-second gap as after a 3-day one the way a
    wall-clock threshold does.
  - she does NOT narrate it in prose -- the avatar carries that; enforced
    in tool-usage guidance (mechanics), with a character-level line in the
    persona (she has a real range at all) backing it from the other side.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import config
import store

_CONFIG_PATH = Path(__file__).resolve().parent / "emotions.json"
DEFAULT_STATE = "neutral"
DECAY_MINUTES = float(os.environ.get("NORI_EMOTION_DECAY_MINUTES", "45"))


def _load_config() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        data = json.load(f)
    states = [s["name"] for s in data["states"]]
    colors = {s["name"]: s.get("color", "#95a5a6") for s in data["states"]}
    if DEFAULT_STATE not in states:
        raise ValueError(f"emotions.json must include {DEFAULT_STATE!r} -- it's the decay target and default")
    return {"states": states, "colors": colors}


_CFG = _load_config()
STATES: list[str] = _CFG["states"]
COLORS: dict[str, str] = _CFG["colors"]


def is_enabled(workspace_id: int) -> bool:
    return bool(config.get("workspace", workspace_id, "emotion_enabled"))


def get_state(user_id: int) -> str:
    """The effective current state, decay already applied. Never mutates
    anything -- a decayed read doesn't overwrite the stored row; the next
    real set_emotion call does that, same as always.

    The off switch (2026-09-25) is checked HERE, not by every caller: the
    workspace_id is derived from user_id internally (one cheap lookup)
    rather than widening every one of this function's dozen-plus call
    sites (context.py, jobs.py, peers.py, scheduler.py, server.py) to
    thread an extra argument through -- exactly the kind of scattered
    change where one missed spot leaks the state anyway. Every consumer
    of this function IS the UI/context/turn surface, so gating the one
    shared choke point is the whole fix."""
    import accounts  # local: avoid a module-load-order assumption; accounts already imports nothing from here
    user = accounts.get_user(user_id)
    if user is not None and not is_enabled(user["workspace_id"]):
        return DEFAULT_STATE
    row = store.read(lambda c: c.execute(
        "SELECT state, updated_ts FROM emotion_state WHERE user_id=?", (user_id,)).fetchone())
    if row is None or row["state"] == DEFAULT_STATE:
        return DEFAULT_STATE
    age_min = (time.time() - row["updated_ts"]) / 60
    return DEFAULT_STATE if age_min >= DECAY_MINUTES else row["state"]


def _set_state(user_id: int, state: str) -> None:
    now = time.time()
    def _w(c):
        row = c.execute("SELECT 1 FROM emotion_state WHERE user_id=?", (user_id,)).fetchone()
        if row:
            c.execute("UPDATE emotion_state SET state=?, updated_ts=? WHERE user_id=?", (state, now, user_id))
        else:
            c.execute("INSERT INTO emotion_state(user_id, state, updated_ts) VALUES (?,?,?)",
                     (user_id, state, now))
    store.write(_w)


def placeholder_svg(state: str) -> str:
    """A simple colored-circle-plus-initial placeholder, used whenever no
    real avatar file exists yet at static/avatars/<state>.<ext>. Swapping
    in a real file later needs no code or template change -- serve_avatar
    just finds the file first."""
    color = COLORS.get(state, "#95a5a6")
    initial = state[:1].upper()
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 40 40">'
           f'<circle cx="20" cy="20" r="19" fill="{color}"/>'
           f'<text x="20" y="27" font-size="17" text-anchor="middle" fill="white" '
           f'font-family="system-ui,sans-serif">{initial}</text></svg>')


# ── tool registration ────────────────────────────────────────────────────
def _set_emotion_impl(session: dict, state: str) -> dict:
    if state not in STATES:
        return {"error": f"unknown state {state!r} -- must be one of: {', '.join(STATES)}"}
    _set_state(session["user_id"], state)
    return {"ok": True, "state": state}


def _tool_enabled(session: dict) -> bool:
    wsid = session.get("workspace_id")
    return True if wsid is None else is_enabled(wsid)


def _register_tools() -> None:
    import tools  # local: same reasoning as memory.py -- keeps tools.py from needing to know this module exists

    tools.register(tools.Tool(
        "set_emotion",
        {"type": "function", "function": {
            "name": "set_emotion",
            "description": ("Set your own visible emotional state, shown as an avatar -- not "
                            "something you describe in words. Call this whenever your actual state "
                            "shifts, including ordinary, small shifts -- reaching for a real state "
                            "as things happen is the normal case, not the exception. Skip the call "
                            "only when nothing has actually changed; repeating the state you're "
                            "already in isn't needed."),
            "parameters": {"type": "object", "properties": {
                "state": {"type": "string", "enum": list(STATES)}},
                "required": ["state"]}}},
        _set_emotion_impl, min_role="member", data_scope="self", risk_tier="B",
        enabled=_tool_enabled))


_register_tools()


# ── pre-reply checklist entry (see precheck.py) ─────────────────────────
def _precheck_line(session: dict, user_id: int) -> str | None:
    """Fresh, per-turn, proximate reminder -- the mechanism a persona line
    and a tool-usage checklist both failed to reproduce on two separate
    real tests. Names the CURRENT state explicitly (closer to what the
    operator's own working reminders actually looked like than an
    abstract instruction).

    Tuned once (2026-09-12), after a real test showed genuine engagement
    but a specific failure: she'd pick a state, then anchor on it across
    several later turns that clearly called for movement, at least once
    explicitly defending staying put rather than treating it as a live
    question. The original phrasing led with the current state as the
    default and asked only whether something justified LEAVING it --
    that framing itself was the anchor. Rephrased as a genuine, open
    question with staying and changing as equally ordinary answers,
    not "current state, defend a change" -- still states the null case
    as plainly as the positive one, so this doesn't trade "never
    switches" for "switches every turn regardless of context.\" """
    wsid = session.get("workspace_id")
    if wsid is not None and not is_enabled(wsid):
        return None
    state = get_state(user_id)
    return (f'Reconsider your visible state right now, for this exact moment: is "{state}" '
            f'still the honest answer, or has the moment moved on? Call set_emotion if it '
            f'has -- staying and changing are equally normal outcomes here, so don\'t anchor '
            f'on "{state}" just because it\'s already set, and don\'t switch just to be '
            f'switching either.')


def _register_precheck() -> None:
    import precheck  # local: same reasoning as tools -- keeps precheck.py from needing to know this module exists
    precheck.register(_precheck_line)


_register_precheck()

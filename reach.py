# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""How the person is reaching their assistant right now (2026-11-10): by voice, or from the car.

Two modes, both decided from where things actually came from, never from anything the person said or the model guessed:

  * car    the person's phone reports Android Auto connected (the poll snapshot's `car`, android_app CarWatcher), or the
           message being answered was spoken to Android Auto (`/device/reply` with via=car, meta source "car"). They are
           driving, and the reply is read aloud by the car.
  * voice  the message being answered came from the chat's voice conversation (meta source "voice"): a transcript of
           speech, and the reply is read aloud.

When one applies, chat.run() adds ONE late system line (after the precheck and the phone glance, so it is proximate) with
the fact and how to answer, and caps the turn's tool rounds (ROUNDS), because a spoken answer that waits on a long chain of
calls is a silence the person cannot see. Car outranks voice. Car also applies to a turn nobody asked for (a reminder or a
ping while they drive), because that reply reaches the car too. Never to a peer-motivated turn: what it writes goes to
another assistant, not to the person.

One switch per person (config `reach_enabled`, Settings -> Devices -> phone). Reads only the person's own messages' `source`
and their own phones' last report.
"""
from __future__ import annotations

import json
import time

import config
import conversation

CAR_STATE_S = 3 * 60      # a phone polls every ~25 s while connected; a report older than this no longer says it is in the car
ROUNDS = {"car": 3, "voice": 4}

FACTS = {
    "car": ("{name} is IN THE CAR: Android Auto is connected on their phone, or their last message was spoken to it. "
            "They are driving, and your reply will be read aloud to them by the car."),
    "voice": ("{name} is TALKING to you by voice: what you read as their message is a transcript of their speech "
              "(a word may be misheard), and your reply will be read aloud to them."),
}
GUIDANCE = {
    "car": ("Answer in one or two short spoken sentences of plain words: nothing that has to be read, looked at or tapped "
            "(no lists, links, tables, emoji or formatting). Don't ask them to look at a screen, approve something on the "
            "phone, or do anything with their hands. If what they want needs an approval or a screen, say so in a few words "
            "and that it can wait until they've stopped. Make as few tool calls as the answer needs."),
    "voice": ("Speak, don't write: one to three short sentences, the way you would say them aloud. No lists, links, "
              "tables, emoji or formatting. Make as few tool calls as the answer needs."),
}


def car_connected(user_id: int) -> bool:
    """One of this person's phones said Android Auto is connected in a recent, unpaused report."""
    import devices   # local: devices -> bridge are loaded after chat
    now = time.time()
    for d in devices.devices_for(user_id, "phone"):
        st = d.get("state") or {}
        if d.get("paused") or d.get("held"):
            continue
        if (st.get("snapshot") or {}).get("car") is True and now - float(st.get("seen_ts") or 0) <= CAR_STATE_S:
            return True
    return False


def _source_of_latest(user_id: int) -> str:
    row = conversation.latest_user_message_after(user_id, 0)
    if not row or not row.get("meta"):
        return ""
    try:
        meta = json.loads(row["meta"])
    except (ValueError, TypeError):
        return ""
    return str(meta.get("source") or "") if isinstance(meta, dict) else ""


def mode(user_id: int, *, answering: bool) -> str | None:
    """'car', 'voice' or None. `answering`: this turn answers the person's own newest message (not a turn nobody asked for)."""
    if not config.get("user", user_id, "reach_enabled"):
        return None
    source = _source_of_latest(user_id) if answering else ""
    if source == "car" or car_connected(user_id):
        return "car"
    if source == "voice":
        return "voice"
    return None


def note(mode_: str | None, display_name: str) -> dict | None:
    if mode_ not in FACTS:
        return None
    name = (display_name or "").strip() or "They"
    return {"role": "system", "content": FACTS[mode_].format(name=name) + " " + GUIDANCE[mode_]}


def capped_rounds(mode_: str | None, rounds: int) -> int:
    cap = ROUNDS.get(mode_ or "")
    return min(rounds, cap) if cap else rounds

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Every approval waiting on a person, from every module, in one place (2026-11-03, operator: "Pending approvals should be visible in chat
when they happen, but any that are still pending should show in the board view. This should apply to all approvals in any module").

Each module that holds an action until a person says yes keeps its own table, expiry and resolve function (infra.py's changes, bridge.py's
browser and phone commands, peers.py's peer-requested actions) and registers a SOURCE here with three callables:

  * pending(sess)            -> what is waiting on this person: [{id, summary, host, created_ts, expires_ts, message_id}]
  * resolve(sess, id, yes)   -> (ok, message, card status); the module's own checks (admin, owner, runs once) stay in the module
  * status(sess, id)         -> the card status now ("pending", "approved", "done", "failed", "denied", "expired", "cancelled"), or None

The chat card (meta.approval on the message that asks, see card()), the board's "waiting for you" list, the card's live status on /poll
and the Approve / Deny buttons on a phone or browser notification all go through this module, so a new kind of approval only has to
register to appear everywhere. A card's `source` names its module; a card with no source is a browser/phone command (bridge.py's cards
predate this module).
"""
from __future__ import annotations

import json

import store

CARD_STATUSES = ("pending", "approved", "done", "failed", "denied", "expired", "cancelled")

# A notification's Approve / Deny button sends back one number (the phone app and the extension were built for bridge.py's command ids).
# A bridge command keeps its own id; any other source is sent as HANDLE_BASE + the id of the chat message that carries its card, which a
# command id never reaches, so the two can never be mistaken for each other.
HANDLE_BASE = 10 ** 12

_SOURCES: dict[str, dict] = {}


def register(source: str, *, label: str, pending, resolve, status) -> None:
    _SOURCES[source] = {"label": label, "pending": pending, "resolve": resolve, "status": status}


def sources() -> list[str]:
    return list(_SOURCES)


def _norm(source: str | None) -> str:
    return source or "bridge"


def card(source: str, aid: int, summary: str, host: str = "", **extra) -> dict:
    """The meta.approval a module puts on the chat message that asks."""
    return {"approval": {"id": aid, "status": "pending", "source": source, "summary": summary, "host": host, **extra}}


def set_card(message_id, status: str) -> None:
    """Keep a chat card in step with its request, so a page loaded (or polled) later shows what happened instead of dead buttons."""
    if not message_id:
        return

    def w(c):
        row = c.execute("SELECT meta FROM messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            return
        try:
            meta = json.loads(row["meta"]) if row["meta"] else {}
        except (ValueError, TypeError):
            meta = {}
        if isinstance(meta.get("approval"), dict):
            meta["approval"]["status"] = status
            c.execute("UPDATE messages SET meta=? WHERE id=?", (json.dumps(meta), message_id))
    store.write(w)


def pending(sess: dict) -> list[dict]:
    """Everything waiting on this person, oldest first. One module failing never hides another's."""
    out: list[dict] = []
    for source, spec in _SOURCES.items():
        try:
            items = spec["pending"](sess) or []
        except Exception as exc:  # noqa: BLE001 -- a list for a person to read; one broken module must not empty it
            print(f"approvals: {source} pending failed: {type(exc).__name__}: {exc}", flush=True)
            continue
        for it in items:
            out.append({"source": source, "label": spec["label"], "host": "", "message_id": None, "expires_ts": None, **it})
    out.sort(key=lambda x: (x.get("created_ts") or 0, x["source"], x["id"]))
    return out


def count(sess: dict) -> int:
    return len(pending(sess))


def resolve(sess: dict, source: str | None, aid, approve: bool) -> tuple[bool, str, str]:
    spec = _SOURCES.get(_norm(source))
    if spec is None:
        return False, "no such kind of request", "error"
    try:
        aid = int(aid)
    except (TypeError, ValueError):
        return False, "bad id", "error"
    return spec["resolve"](sess, aid, bool(approve))


def status(sess: dict, source: str | None, aid) -> str | None:
    spec = _SOURCES.get(_norm(source))
    if spec is None:
        return None
    try:
        return spec["status"](sess, int(aid))
    except (TypeError, ValueError):
        return None


def statuses(sess: dict, keys: list[str], limit: int = 50) -> dict[str, str]:
    """For /poll: the current status of the cards a page is showing as pending ("source:id"), so a decision made somewhere else (the
    board, a phone, another tab) or an expiry reaches the card without a reload."""
    out: dict[str, str] = {}
    for key in keys[:limit]:
        src, _, raw = key.rpartition(":")
        st = status(sess, src, raw)
        if st:
            out[key] = st
    return out


def notify_handle(source: str | None, aid: int, message_id: int) -> int:
    return int(aid) if _norm(source) == "bridge" else HANDLE_BASE + int(message_id)


def from_handle(user_id: int, handle: int) -> tuple[str, int] | None:
    """A notification button's number back to (source, id). Only the person's own messages resolve."""
    if handle < HANDLE_BASE:
        return "bridge", handle
    row = store.read(lambda c: c.execute("SELECT meta FROM messages WHERE id=? AND user_id=?", (handle - HANDLE_BASE, user_id)).fetchone())
    if row is None:
        return None
    try:
        a = (json.loads(row["meta"]) if row["meta"] else {}).get("approval")
    except (ValueError, TypeError, AttributeError):
        return None
    if not isinstance(a, dict) or a.get("id") is None:
        return None
    return _norm(a.get("source")), int(a["id"])

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""One place to look for "why was there no reply?" and "why was my service
down?" (2026-09-18).

Both questions used to be answered from separate places -- the reply_requested
decision log, the round-limit log, the (new) model-failure log, failed
peer-tool rows buried in the message stream, and a supervisor whose outcome
was only in a file nobody opened. An operator asking either question is
after the same kind of answer -- what happened, when, and why -- so this
module normalises all of them into ONE chronological list of events:

    {"ts": float, "source": str, "ok": bool, "title": str, "detail": str,
     "peer": str | None}

`ok=False` marks a problem (a dropped request, a hit limit, a failed model
call, a failed peer send, a service that was down). `ok=True` rows are kept
because "the request was granted and ran" is what makes a nearby failure
readable, and a service that came back is as important as one that went down.

Pure read side: nothing here writes, and every source degrades to "no rows"
rather than raising -- a diagnostics page that can itself fail is worse than
none.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import store

SUPERVISION_LOG = Path(__file__).resolve().parent / ".supervision.jsonl"

_COMPULSION_LABELS = {
    "granted": ("ran immediately", True), "queued": ("queued (busy)", True),
    "run_from_queue": ("ran from queue", True),
    "dropped_expired": ("dropped (expired while queued)", False),
    "dropped_other": ("dropped", False),
}


def _peer_names() -> dict[int, str]:
    try:
        return {r["id"]: r["name"] for r in store.read(
            lambda c: c.execute("SELECT id, name FROM peers").fetchall())}
    except Exception:
        return {}


def _safe(fn):
    try:
        return fn()
    except Exception:
        return []


def _compulsion(names, limit):
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM peer_compulsion_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall())
    out = []
    for r in rows:
        label, ok = _COMPULSION_LABELS.get(r["decision"], (r["decision"], True))
        try:
            n = len(json.loads(r["message_ids"]))
        except Exception:
            n = 1
        out.append({"ts": r["ts"], "source": "reply_requested", "ok": ok,
                    "title": label + (f" ({n} messages)" if n > 1 else ""),
                    "detail": r["detail"] or "", "peer": names.get(r["peer_id"])})
    return out


def _round_limit(names, limit):
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM peer_turn_limit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall())
    return [{"ts": r["ts"], "source": "round limit", "ok": False,
             "title": f"turn hit its {r['rounds']}-round limit",
             "detail": r["reason"] or "", "peer": names.get(r["peer_id"])} for r in rows]


def _model_failures(names, limit):
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM peer_model_failure_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall())
    out = []
    for r in rows:
        why = f" (turn was for: {r['reason']})" if r["reason"] else ""
        out.append({"ts": r["ts"], "source": "model call", "ok": False,
                    "title": "model call failed after retries -- no reply was produced",
                    "detail": (r["error"] or "") + why, "peer": names.get(r["peer_id"])})
    return out


_PEER_TOOL = re.compile(r"^peer(\d+)_(send|check|act)$|^peer_(send|check|act)$")


def _failed_peer_tools(names, limit):
    # Structured failure detail lives in messages.meta ({tool_name, failed,
    # error}) -- the visible "used peer3_send" line deliberately stays as-is.
    rows = store.read(lambda c: c.execute(
        "SELECT ts, meta FROM messages WHERE kind='tool' AND meta IS NOT NULL "
        "AND meta LIKE '%\"failed\"%' ORDER BY id DESC LIMIT ?", (limit * 4,)).fetchall())
    out = []
    for r in rows:
        try:
            meta = json.loads(r["meta"])
        except Exception:
            continue
        tool = str(meta.get("tool_name") or "")
        m = _PEER_TOOL.match(tool)
        if not m or not meta.get("failed"):
            continue
        peer = names.get(int(m.group(1))) if m.group(1) else None
        out.append({"ts": r["ts"], "source": "peer tool", "ok": False,
                    "title": f"{tool} failed", "detail": str(meta.get("error") or ""), "peer": peer})
        if len(out) >= limit:
            break
    return out


def _supervision(path, limit):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-limit * 3:]:
        try:
            e = json.loads(line)
            ts = time.mktime(time.strptime(str(e["ts"])[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        kind = e.get("kind") or "event"
        bits = [str(e.get("detail") or "")]
        if e.get("crash"):
            bits.append("crash: " + str(e["crash"])[-400:])
        if e.get("mismatch"):
            bits.append("the control script reported success but nothing was serving")
        if e.get("ctl_output") and not e.get("ok"):
            bits.append("ctl said: " + str(e["ctl_output"])[-300:])
        out.append({"ts": ts, "source": "supervisor", "ok": bool(e.get("ok")),
                    "title": f"{kind} by {e.get('caller') or 'manual'}"
                             + (" -- serving" if e.get("ok") else " -- DOWN / failed"),
                    "detail": " | ".join(b for b in bits if b), "peer": None})
    return out


def events(limit: int = 40, supervision_path=None) -> list[dict]:
    """Newest first, across every source, capped at `limit`."""
    names = _peer_names()
    every = []
    for fn in (_compulsion, _round_limit, _model_failures, _failed_peer_tools):
        every += _safe(lambda fn=fn: fn(names, limit))
    every += _safe(lambda: _supervision(supervision_path or SUPERVISION_LOG, limit))
    every.sort(key=lambda e: e["ts"], reverse=True)
    return every[:limit]

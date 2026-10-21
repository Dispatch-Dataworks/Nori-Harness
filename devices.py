# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Devices (2026-10-21; docs/devices.md): the Android app and the browser extension.

This module is the registry, the pairing, and the delivery plumbing both share:

  * pairing -- a one-time code (hashed, 15 minutes, single use) that becomes a per-device bearer token;
  * the registry -- bridge_tokens, with kind (browser|phone), the device's own pause, its reported consent
    (caps), its last state (battery, place, the windows its OWNER opened on it), and a notification cursor;
  * delivery -- which of her messages become a notification, once, at the right moment (suppressed while the app
    is in front, in quiet hours, or for replies the owner turned off), advanced only on an explicit ack;
  * the phone's long poll -- messages and at most one command per poll, with the phone's server-side gate
    re-checked AT HAND-OUT (a switch flipped while a command waited fails it rather than sending it);
  * the settings pushed to the phone, the windows, the per-hour cap, and the device activity log.

What the phone DOES with its data lives in device_data.py (health, places, shares, "ask about this page") and what
she can do TO the phone in phone_tools.py. The principles (docs/devices.md): outbound only; per-user ownership; two
keys for every capability (server switch + the device's own consent, re-checked on the device); off by default;
everything read off a screen is untrusted data; risky actions pause for a human; bounded and logged.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time

import bridge
import config
import conversation
import store
import usertime

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"      # no 0/O/1/I/L
CODE_LEN = 8
CODE_TTL_S = 15 * 60
MAX_LIVE_CODES = 5
MAX_DEVICES = {"phone": 6, "browser": 12}
KNOWN_CAPS = ("notifications", "share", "health", "places", "actions", "screen", "control")
PHONE_POLL_WAIT_S = 25
NOTIFY_KINDS = ("chat", "proactive", "reminder", "job_proactive", "peer_proactive", "image")
MAX_MESSAGES_PER_POLL = 20
STALE_MESSAGE_S = 6 * 3600            # a notification this old is no longer worth interrupting for
REDELIVER_AFTER_S = 4
MAX_DELIVERIES = 3
ONLINE_S = 60
IDLE_S = 15 * 60
HEALTH_METRICS = ("weight", "bp", "steps", "sleep", "resting_hr")
# What each phone command needs from the SERVER side (the phone enforces its own half).
_ACTION_NEEDS = {"open_url": "actions", "open_app": "actions", "set_timer": "actions", "set_alarm": "actions",
                 "navigate": "actions", "list_apps": "actions", "read_screen": "control", "interact": "control",
                 "phone_screenshot": "screen"}
_NEED_SETTING = {"actions": "phone_actions_enabled", "control": "phone_control_enabled", "screen": "phone_screen_enabled"}
_WINDOW_FOR = {"control": "interact", "screen": "share"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json(raw, default):
    try:
        v = json.loads(raw) if raw else default
    except (ValueError, TypeError):
        return default
    return v if isinstance(v, type(default)) else default


# ── pairing ──────────────────────────────────────────────────────────────
def _norm_code(code: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", code or "").upper()


def _code_hash(norm: str) -> str:
    return hashlib.sha256(("nori-pair:" + norm).encode()).hexdigest()


def create_code(user_id: int, workspace_id: int, kind: str) -> tuple[str | None, str]:
    """A one-time pairing code, shown once as XXXX-XXXX. (code, "") or (None, error)."""
    if kind not in bridge.KINDS:
        return None, "unknown device kind"
    now = time.time()
    store.write(lambda c: c.execute("DELETE FROM device_pairing_codes WHERE expires_ts<?", (now - 86400,)))
    live = store.read(lambda c: c.execute(
        "SELECT COUNT(*) n FROM device_pairing_codes WHERE user_id=? AND used_ts IS NULL AND expires_ts>?",
        (user_id, now)).fetchone())["n"]
    if live >= MAX_LIVE_CODES:
        return None, f"there are already {MAX_LIVE_CODES} unused codes -- use one or wait for them to expire"
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    store.write(lambda c: c.execute(
        "INSERT INTO device_pairing_codes(user_id, workspace_id, kind, code_hash, created_ts, expires_ts) "
        "VALUES (?,?,?,?,?,?)", (user_id, workspace_id, kind, _code_hash(raw), now, now + CODE_TTL_S)))
    return f"{raw[:4]}-{raw[4:]}", ""


def claim_code(code: str, kind: str, *, name: str = "", version: str = "") -> tuple[dict | None, str]:
    """Trade a pairing code for a device token. ({"token", "name"}, "") or (None, reason). The reason is the same
    wording whether the code is wrong, expired, used, or for the other kind of device -- nothing to probe."""
    bad = "that code isn't valid, or it has expired"
    norm = _norm_code(code)
    if len(norm) != CODE_LEN or kind not in bridge.KINDS:
        return None, bad
    now = time.time()
    row = store.read(lambda c: c.execute(
        "SELECT * FROM device_pairing_codes WHERE code_hash=? AND used_ts IS NULL AND expires_ts>? AND kind=?",
        (_code_hash(norm), now, kind)).fetchone())
    if row is None:
        return None, bad
    active = store.read(lambda c: c.execute(
        "SELECT COUNT(*) n FROM bridge_tokens WHERE user_id=? AND kind=? AND revoked_ts IS NULL",
        (row["user_id"], kind)).fetchone())["n"]
    if active >= MAX_DEVICES[kind]:
        return None, f"this account already has {MAX_DEVICES[kind]} paired {kind}s -- remove one first"
    used = store.write(lambda c: c.execute(
        "UPDATE device_pairing_codes SET used_ts=? WHERE id=? AND used_ts IS NULL", (now, row["id"])).rowcount)
    if not used:
        return None, bad                               # lost a race: single use means single use
    label = re.sub(r"[\x00-\x1f]", " ", name or "").strip()[:60] or kind
    raw = bridge.create_token(row["user_id"], row["workspace_id"], label, kind)
    cursor = conversation.max_id(row["user_id"])       # nothing from before pairing is ever replayed
    store.write(lambda c: c.execute(
        "UPDATE bridge_tokens SET last_msg_id=?, app_version=? WHERE token_hash=?",
        (cursor, (version or "")[:20], bridge._hash(raw))))
    log_activity(row["user_id"], None, "paired", f"{kind}: {label}")
    return {"token": raw, "name": label}, ""


# ── the registry ─────────────────────────────────────────────────────────
def get_device(device_id: int) -> dict | None:
    r = store.read(lambda c: c.execute("SELECT * FROM bridge_tokens WHERE id=?", (device_id,)).fetchone())
    return dict(r) if r else None


def device_state(row: dict) -> dict:
    return _json(row.get("state"), {})


def devices_for(user_id: int, kind: str | None = None) -> list[dict]:
    """The user's live devices, each with a computed status (online / idle / offline), for the Devices page."""
    now = time.time()
    out = []
    for t in bridge.list_tokens(user_id, kind):
        if t["revoked_ts"]:
            continue
        st = _json(t.get("state"), {})
        age = now - (t["last_seen_ts"] or 0) if t["last_seen_ts"] else None
        status = ("never seen" if age is None else "online" if age <= ONLINE_S else "idle" if age <= IDLE_S else "offline")
        out.append({**t, "status": status, "caps": _json(t.get("caps"), {}), "state": st, "held": bool(st.get("server_hold"))})
    return out


def set_hold(user_id: int, device_id: int, hold: bool) -> bool:
    """The server-side hold: nothing is delivered to, accepted from, or run on this device while it's set."""
    d = get_device(device_id)
    if d is None or d["user_id"] != user_id or d["revoked_ts"]:
        return False
    st = device_state(d)
    st["server_hold"] = bool(hold)
    store.write(lambda c: c.execute("UPDATE bridge_tokens SET state=? WHERE id=?", (json.dumps(st), device_id)))
    with bridge._cond:
        bridge._cond.notify_all()
    return True


def is_held(token: dict) -> bool:
    d = get_device(token["id"])
    return bool(d and device_state(d).get("server_hold"))


def revoke(user_id: int, device_id: int) -> bool:
    d = get_device(device_id)
    if d is None or d["user_id"] != user_id:
        return False
    bridge.revoke_token(device_id)
    log_activity(user_id, device_id, "revoked", d["label"])
    return True


def _clean_snapshot(s) -> dict:
    s = s if isinstance(s, dict) else {}
    out: dict = {}
    if isinstance(s.get("battery"), (int, float)):
        out["battery"] = max(0, min(100, int(s["battery"])))
    for k in ("charging", "screen_on", "locked", "dnd"):
        if isinstance(s.get(k), bool):
            out[k] = s[k]
    for k, n in (("network", 16), ("place", 60)):
        if isinstance(s.get(k), str):
            out[k] = re.sub(r"[\x00-\x1f]", " ", s[k])[:n]
    return out


def update_from_poll(token: dict, body: dict) -> None:
    """Record what the device just told us about itself: its consent (caps), the windows its owner opened ON the
    phone, a minimal status snapshot, whether it's paused, and its app version."""
    d = get_device(token["id"]) or {}
    st = device_state(d)
    caps = {k: bool(v) for k, v in (body.get("caps") or {}).items() if k in KNOWN_CAPS} if isinstance(body.get("caps"), dict) else _json(d.get("caps"), {})
    for k in ("interact_until", "share_until"):
        v = body.get(k)
        st[k] = int(v) if isinstance(v, (int, float)) and v > 0 else 0
    st["app_visible"] = bool(body.get("app_visible"))
    if "snapshot" in body:
        st["snapshot"] = _clean_snapshot(body.get("snapshot"))
    st["seen_ts"] = time.time()
    paused = 1 if body.get("paused") else 0
    ver = str(body.get("v") or d.get("app_version") or "")[:20]
    store.write(lambda c: c.execute(
        "UPDATE bridge_tokens SET caps=?, state=?, paused=?, app_version=? WHERE id=?",
        (json.dumps(caps), json.dumps(st), paused, ver, token["id"])))


def mark_paused(device_id: int, paused: bool) -> None:
    """The device reported its own pause (the browser's poll; the phone's is recorded in update_from_poll)."""
    store.write(lambda c: c.execute("UPDATE bridge_tokens SET paused=? WHERE id=?", (1 if paused else 0, device_id)))


def window_open(user_id: int, which: str) -> bool:
    """True if a phone of this user is connected, not paused/held, and reports its owner's `which` window
    ("interact" or "share") still open. The windows are opened ONLY on the phone."""
    now_ms = _now_ms()
    for t in devices_for(user_id, "phone"):
        if t["paused"] or t["held"] or t["status"] not in ("online",):
            continue
        if int(t["state"].get(which + "_until") or 0) > now_ms:
            return True
    return False


def phone_device(user_id: int) -> dict | None:
    """The user's most recently seen live phone, or None."""
    ds = devices_for(user_id, "phone")
    ds.sort(key=lambda d: d["last_seen_ts"] or 0, reverse=True)
    return ds[0] if ds else None


# ── activity log (counts and ids -- never content) ───────────────────────
def log_activity(user_id: int, device_id: int | None, kind: str, detail: str = "") -> None:
    try:
        store.write(lambda c: c.execute(
            "INSERT INTO device_activity(ts, user_id, device_id, kind, detail) VALUES (?,?,?,?,?)",
            (time.time(), user_id, device_id, kind, (detail or "")[:200])))
    except Exception as exc:  # noqa: BLE001 -- a log failure must not fail the thing being logged
        print(f"devices.log_activity: {type(exc).__name__}: {exc}", flush=True)


def recent_activity(user_id: int, limit: int = 40) -> list[dict]:
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT * FROM device_activity WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall())]


# ── notification delivery ────────────────────────────────────────────────
def _plain(text: str, cap: int = 400) -> str:
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text or "")
    t = re.sub(r"[*_`#>]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= cap else t[:cap - 1].rstrip() + "…"


def in_quiet_hours(user_id: int) -> bool:
    if not config.get("user", user_id, "notify_enabled"):
        return True
    start, end = config.get("user", user_id, "notify_quiet_start"), config.get("user", user_id, "notify_quiet_end")
    h = usertime.local_dt(user_id).hour
    if start == end:
        return False
    return (start <= h < end) if start < end else (h >= start or h < end)


def category_of(row: dict) -> str | None:
    """'chat' | 'ping' | 'reminder' | 'job' | 'approval' | 'peer' for one of her messages, or None if it isn't a
    notification candidate."""
    kind = row.get("kind")
    if row.get("role") != "assistant" or kind not in NOTIFY_KINDS:
        return None
    meta = _json(row.get("meta"), {})
    if isinstance(meta.get("approval"), dict):
        return "approval"
    return {"chat": "chat", "image": "chat", "proactive": "ping", "reminder": "reminder",
            "job_proactive": "job", "peer_proactive": "peer"}[kind]


def _cursor(device_id: int) -> int:
    d = get_device(device_id)
    return int(d["last_msg_id"]) if d and d["last_msg_id"] is not None else 0


def _set_cursor(device_id: int, upto: int) -> None:
    store.write(lambda c: c.execute(
        "UPDATE bridge_tokens SET last_msg_id=? WHERE id=? AND COALESCE(last_msg_id,0)<?", (upto, device_id, upto)))


def _notify_enabled(user_id: int, kind: str) -> bool:
    return bool(config.get("user", user_id, "phone_notifications_enabled" if kind == "phone" else "browser_notifications_enabled"))


def _candidates(device_id: int, user_id: int, after: int) -> list[dict]:
    return conversation.assistant_messages_after(user_id, after, NOTIFY_KINDS, 50)


def pending_messages(token: dict, *, app_visible: bool = False, advance_empty: bool = True) -> list[dict]:
    """The notifications to hand this device now (docs/devices.md): oldest first, at most 20. Does NOT advance the
    cursor for what it returns -- that happens on the device's ack -- but it does move the cursor over messages that
    will never be delivered (the app is in front, the owner turned the channel off, everything was filtered), so a
    stale one can't surface hours later."""
    user_id, device_id, kind = token["user_id"], token["id"], token.get("kind", "browser")
    after = _cursor(device_id)
    rows = _candidates(device_id, user_id, after)
    if not rows:
        return []
    newest = rows[-1]["id"]
    if app_visible or not _notify_enabled(user_id, kind):
        if advance_empty:
            _set_cursor(device_id, newest)
        return []
    quiet = in_quiet_hours(user_id)
    replies_on = bool(config.get("user", user_id, "phone_notify_replies"))
    now = time.time()
    name = config.get("workspace", token["workspace_id"], "assistant_name")
    out: list[dict] = []
    for r in rows:
        cat = category_of(r)
        if cat is None or now - r["ts"] > STALE_MESSAGE_S:
            continue
        meta = _json(r.get("meta"), {})
        approval = None
        if cat == "approval":
            a = meta["approval"]
            if a.get("status") != "pending":
                continue                       # already decided somewhere else
            approval = {"id": a.get("id"), "summary": _plain(a.get("summary") or "", 120), "host": _plain(a.get("host") or "", 80)}
        if quiet and cat not in ("reminder", "approval"):
            continue
        if cat == "chat" and not replies_on:
            continue
        text = ("Waiting for your OK: " + approval["summary"]) if approval else _plain(r["content"]) or "(sent you a picture)"
        out.append({"id": r["id"], "category": cat, "title": name, "text": text, "ts": int(r["ts"] * 1000),
                    "approval": approval})
        if len(out) >= MAX_MESSAGES_PER_POLL:
            break
    if not out and advance_empty:
        _set_cursor(device_id, newest)         # examined, nothing deliverable: never revisit
    return out


def apply_ack(token: dict, upto) -> None:
    if isinstance(upto, bool) or not isinstance(upto, (int, float)) or upto <= 0:
        return
    _set_cursor(token["id"], int(upto))


def has_pending(token: dict, *, app_visible: bool = False) -> bool:
    """Cheap 'is there something to deliver' for the long-poll loops. Never advances the cursor."""
    return bool(pending_messages(token, app_visible=app_visible, advance_empty=False))


# ── the settings the phone must obey ─────────────────────────────────────
def _csv(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()][:200]


def phone_settings(user_id: int) -> dict:
    from device_data import list_places   # local: device_data imports this module
    g = lambda k: config.get("user", user_id, k)  # noqa: E731
    return {
        "notifications": bool(g("phone_notifications_enabled")),
        "health": bool(g("phone_health_enabled")),
        "places": bool(g("phone_places_enabled")),
        "actions": bool(g("phone_actions_enabled")),
        "control": bool(g("phone_control_enabled")),
        "screen": bool(g("phone_screen_enabled")),
        "quiet": in_quiet_hours(user_id),
        "hidden_apps": _csv(g("phone_hidden_apps")),
        "never_control_apps": [],
        "health_metrics": list(HEALTH_METRICS),
        "places_list": [{"id": p["id"], "name": p["name"], "lat": p["lat"], "lon": p["lon"],
                         "radius_m": p["radius_m"], "home": bool(p["is_home"])} for p in list_places(user_id)],
        "actions_per_hour": int(g("phone_actions_per_hour") or 30),
    }


def browser_settings(user_id: int) -> dict:
    g = lambda k: config.get("user", user_id, k)  # noqa: E731
    return {"notifications": bool(g("browser_notifications_enabled")), "activity": bool(g("browser_activity_enabled")),
            "hidden_hosts": _csv(g("browser_hidden_hosts")), "standing_look": bool(g("bridge_standing_look"))}


# ── APK distribution ─────────────────────────────────────────────────────
def apk_dir():
    return store.DATA_DIR / "phone_app"


def apk_path():
    p = apk_dir() / "nori-companion.apk"
    return p if p.is_file() else None


def apk_version() -> str | None:
    try:
        v = (apk_dir() / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return v[:20] or None


def app_info() -> dict | None:
    v = apk_version()
    return {"latest": v, "apk_url": "/device/app.apk"} if v and apk_path() else None


# ── the phone's server-side gate (the phone enforces its own half) ───────
def safe_public_url(url: str) -> str | None:
    """The URL if it's a plain public http(s) address, else None. No userinfo, no other schemes, no loopback /
    private / link-local / .local / .internal hosts or IP literals."""
    import ipaddress
    import urllib.parse
    if not isinstance(url, str) or len(url) > 2000 or re.search(r"[\x00-\x20\x7f]", url):
        return None                      # no whitespace or control characters anywhere, including the ends
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
        return None
    if host in ("localhost",) or host.endswith((".local", ".localhost", ".internal", ".lan", ".home", ".corp")):
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url if "." in host else None
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified):
        return None
    return url


def _actions_in_last_hour(user_id: int) -> int:
    return store.read(lambda c: c.execute(
        "SELECT COUNT(*) n FROM bridge_commands WHERE user_id=? AND target='phone' AND created_ts>? "
        "AND status NOT IN ('cancelled')", (user_id, time.time() - 3600)).fetchone())["n"]


def phone_gate(session: dict, need: str) -> dict | None:
    """Why she can't use the phone for `need` ('actions' | 'control' | 'screen') right now, as a tool-shaped error,
    or None. Checked when the tool is called AND again when the command is handed out."""
    if session.get("role") != "admin":
        return {"error": "not permitted for this account"}
    uid = session["user_id"]
    dev = phone_device(uid)
    if dev is None:
        return {"error": "no phone is paired -- pair one in Settings → Devices"}
    if dev["held"]:
        return {"error": "this phone is on hold in Settings → Devices"}
    if dev["paused"]:
        return {"error": "the phone's own pause is on -- the owner can resume it from the notification or tile"}
    if not bridge.connected(uid, "phone"):
        return {"error": "the phone isn't connected right now (asleep, offline, or the app isn't running)"}
    if not config.get("user", uid, _NEED_SETTING[need]):
        return {"error": f"that's switched off in Settings → Devices ({_NEED_SETTING[need].replace('phone_', '').replace('_enabled', '')})"}
    if not dev["caps"].get(need):
        return {"error": f"the phone hasn't allowed '{need}' itself -- the owner turns that on in the Nori app"}
    win = _WINDOW_FOR.get(need)
    if win and int(dev["state"].get(win + "_until") or 0) <= _now_ms():
        return {"error": (f"the phone's {win} window is closed -- the owner opens one on the phone "
                          "(Quick Settings tile or the app) for 15 minutes, an hour, or three")}
    cap = int(config.get("user", uid, "phone_actions_per_hour") or 30)
    if _actions_in_last_hour(uid) >= cap:
        return {"error": f"that's the limit of {cap} phone actions per hour -- try again later"}
    return None


# ── the phone's long poll ────────────────────────────────────────────────
def _deliver_check(user_id: int, cmd: dict) -> str | None:
    need = _ACTION_NEEDS.get(cmd["action"])
    if need is None:
        return "unknown phone action"
    if not config.get("user", user_id, _NEED_SETTING[need]):
        return "switched off in Settings → Devices before it could be sent"
    win = _WINDOW_FOR.get(need)
    if win and not window_open(user_id, win):
        return f"the phone's {win} window closed before this could be sent"
    return None


def _redeliver_due(user_id: int, token_id: int) -> dict | None:
    """A command handed out but never acked (a dropped response) goes out again after a few seconds, at most twice
    more; the phone dedupes by id."""
    now = time.time()

    def _w(c):
        row = c.execute(
            "SELECT * FROM bridge_commands WHERE user_id=? AND target='phone' AND status='claimed' AND acked_ts IS NULL "
            "AND claimed_ts<? AND delivered<? ORDER BY id LIMIT 1",
            (user_id, now - REDELIVER_AFTER_S, MAX_DELIVERIES - 1)).fetchone()    # the first send isn't counted
        if row is None:
            return None
        c.execute("UPDATE bridge_commands SET claimed_ts=?, delivered=delivered+1, token_id=? WHERE id=?",
                  (now, token_id, row["id"]))
        return dict(row)
    return store.write(_w)


def _wire_command(row: dict) -> dict:
    action = "screenshot" if row["action"] == "phone_screenshot" else row["action"]
    return {"id": row["id"], "action": action, "args": json.loads(row["args"]),
            "approved": row["approval"] == "approved", "force_approval": bool(row["force_approval"])}


def ack_command(token: dict, cmd_id) -> bool:
    try:
        cid = int(cmd_id)
    except (TypeError, ValueError):
        return False
    return bool(store.write(lambda c: c.execute(
        "UPDATE bridge_commands SET acked_ts=? WHERE id=? AND user_id=? AND target='phone' AND acked_ts IS NULL",
        (time.time(), cid, token["user_id"])).rowcount))


def phone_poll(token: dict, body: dict, wait_s: float | None = None) -> dict:
    """POST /device/poll: record the device's state, then hold up to 25 s for a command or a notification."""
    user_id = token["user_id"]
    update_from_poll(token, body)
    apply_ack(token, body.get("ack"))
    app_visible = bool(body.get("app_visible"))
    base = {"settings": phone_settings(user_id), "app": app_info()}
    if body.get("paused") or is_held(token):
        return {"active": False, "paused": bool(body.get("paused")), "held": is_held(token), "messages": [],
                "command": None, **base}
    deadline = time.time() + (PHONE_POLL_WAIT_S if wait_s is None else wait_s)
    bridge.sweep()
    while True:
        cmd = _redeliver_due(user_id, token["id"]) or bridge._claim(user_id, token["id"], "phone")
        while cmd is not None:
            why = _deliver_check(user_id, cmd)
            if why is None:
                return {"active": True, "messages": pending_messages(token, app_visible=app_visible),
                        "command": _wire_command(cmd), **base}
            # the gate changed while it waited: fail it, tell whoever is waiting, and look for the next one
            store.write(lambda c: c.execute(
                "UPDATE bridge_commands SET status='failed', error=?, done_ts=? WHERE id=?", (why, time.time(), cmd["id"])))
            ev = bridge._waiters.get(cmd["id"])
            if ev is not None:
                ev.set()
            cmd = bridge._claim(user_id, token["id"], "phone")
        msgs = pending_messages(token, app_visible=app_visible)
        if msgs:
            return {"active": True, "messages": msgs, "command": None, **base}
        if time.time() >= deadline:
            return {"active": True, "messages": [], "command": None, **base}
        with bridge._cond:
            bridge._cond.wait(timeout=min(1.0, max(0.05, deadline - time.time())))


def phone_result(token: dict, body: dict) -> tuple[bool, str]:
    """POST /device/result: the phone's answer to a command (same completion path as the browser's)."""
    try:
        cid = int(body.get("id"))
    except (TypeError, ValueError):
        return False, "bad command id"
    ok, msg = bridge.complete(token, cid, body)
    if ok:
        cmd = bridge._cmd(cid)
        log_activity(token["user_id"], token["id"], "phone_command",
                     f"{cmd['action']}: {(cmd['status'])}" if cmd else "")
    return ok, msg


# ── browser extension additions to /bridge/poll ──────────────────────────
def browser_extras(token: dict, body: dict) -> dict:
    """What the extension's poll gains: notification messages and its settings. `tab_visible` (a Nori tab is the
    focused tab) suppresses replies the same way the phone's app_visible does."""
    user_id = token["user_id"]
    apply_ack(token, body.get("ack"))
    if is_held(token):
        return {"messages": [], "settings": {**browser_settings(user_id), "notifications": False, "activity": False}}
    return {"messages": pending_messages(token, app_visible=bool(body.get("tab_visible"))),
            "settings": browser_settings(user_id)}


def browser_has_pending(token: dict, body: dict):
    visible = bool(body.get("tab_visible"))
    return lambda: has_pending(token, app_visible=visible)


# ── passive browsing time (host + seconds only) ──────────────────────────
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
MAX_SECONDS_PER_HOST_PER_REPORT = 120


def hidden_hosts(user_id: int) -> list[str]:
    return _csv(config.get("user", user_id, "browser_hidden_hosts"))


def add_browser_activity(user_id: int, items) -> int:
    """Add the extension's per-interval host seconds to today's totals. Host names only -- anything that isn't a bare
    hostname is dropped (a URL or title can't ride in), hidden hosts are dropped, seconds are capped per report.
    Returns how many seconds were recorded."""
    if not config.get("user", user_id, "browser_activity_enabled") or not isinstance(items, list):
        return 0
    hidden = hidden_hosts(user_id)
    day = usertime.fmt(user_id, time.time(), "%Y-%m-%d")
    total = 0
    merged: dict[str, int] = {}
    for it in items[:60]:
        if not isinstance(it, dict):
            continue
        host = str(it.get("host") or "").lower().strip().rstrip(".")
        try:
            secs = int(it.get("seconds"))
        except (TypeError, ValueError):
            continue
        if secs <= 0 or not HOST_RE.match(host) or any(bridge.host_matches(host, h) for h in hidden):
            continue
        merged[host] = merged.get(host, 0) + min(secs, MAX_SECONDS_PER_HOST_PER_REPORT)
    for host, secs in merged.items():
        store.write(lambda c, host=host, secs=secs: c.execute(
            "INSERT INTO browser_day(user_id, day, host, seconds) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, day, host) DO UPDATE SET seconds=seconds+excluded.seconds", (user_id, day, host, secs)))
        total += secs
    if total:
        prune_browser_day(user_id)
    return total


def prune_browser_day(user_id: int) -> None:
    keep = max(1, int(config.get("user", user_id, "browser_day_keep_days") or 60))
    cutoff = usertime.fmt(user_id, time.time() - keep * 86400, "%Y-%m-%d")
    store.write(lambda c: c.execute("DELETE FROM browser_day WHERE user_id=? AND day<?", (user_id, cutoff)))


def browser_day(user_id: int, days: int = 1) -> dict:
    """{day: [{host, seconds}, ...]} for the last `days` local days, biggest first."""
    days = max(1, min(int(days or 1), 90))
    first = usertime.fmt(user_id, time.time() - (days - 1) * 86400, "%Y-%m-%d")
    rows = store.read(lambda c: c.execute(
        "SELECT day, host, seconds FROM browser_day WHERE user_id=? AND day>=? ORDER BY day DESC, seconds DESC",
        (user_id, first)).fetchall())
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(r["day"], []).append({"host": r["host"], "seconds": r["seconds"]})
    return out


def clear_browser_day(user_id: int) -> int:
    return int(store.write(lambda c: c.execute("DELETE FROM browser_day WHERE user_id=?", (user_id,)).rowcount) or 0)

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The debug endpoint (2026-10-30, operator's own ask): a read-only way for the person developing Nori to ask a running agent how it is doing --
its health, its recent errors and logs, its conversations, its settings -- without getting at the container.

THIS EXPOSES PRIVATE DATA (the household's conversations and memory), so every rule below is load-bearing:

  * OFF BY DEFAULT, and off means off: while the switch is off (or its optional timer has run out) /debug/... answers 404, identical to any unknown
    path, even to the right token. An admin turns it on in Settings -> Debug, and a header chip says so on every page while it is on.
  * A TOKEN, set by the admin in that same tab (at least 24 characters). Only its SHA-256 is stored, never the token. The same token can be entered on
    every agent: the tab shows an 8-character fingerprint so they can be compared without revealing it. Sent as `Authorization: Bearer <token>`, never in
    the URL (URLs end up in logs). Compared in constant time. Failures count toward the same per-address limit the login form uses.
  * READ-ONLY: GET only, no section changes anything, there is no way to run SQL, shell or tools through it.
  * NO SECRETS: every response is built from explicit column lists (never SELECT *), then passed through scrub(), which redacts any field whose NAME
    says secret (key, token, password, psk, hash, *_enc, cookie, ...) and any string that LOOKS like one (a bearer header, an sk-... key, a connection
    string, a session cookie, a JWT), including inside conversation text and log lines. Settings come from config.readable_spec(), which already
    excludes everything marked secret. Where it can't be sure, it over-redacts.
  * NOT REACHABLE BY THE AGENT: the switch, the token hash and the timer are config entries marked secret, so no tool (get_settings, update_settings,
    a peer's request) can read or change them, and a prompt-injected message cannot switch the endpoint on.
  * AUDITED: each request is recorded (time, address, path, outcome; the last 1000 kept), shown in Settings -> Debug, and printed to the log.

Sections are listed at GET /debug. See docs/debug.md.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import sys
import threading
import time
import urllib.parse

import accounts
import config
import logbuf
import store

MIN_TOKEN_LEN = 24
MAX_ROWS = 500
MAX_RESPONSE_BYTES = 4_000_000
AUDIT_KEEP = 1000
STARTED_TS = time.time()

# The three entries are secret=True on purpose: that is what keeps them out of config.readable_spec(), and so out of every tool the agent has.
config._SPEC["debug_enabled"] = (False, bool, "workspace", True, "debug endpoint switch")
config._SPEC["debug_until_ts"] = (0.0, float, "workspace", True, "debug endpoint switches itself off at this time (0 = stays on)")
config._SPEC["debug_token_hash"] = ("", str, "workspace", True, "SHA-256 of the debug endpoint token")


# ── the switch and the token ─────────────────────────────────────────────
def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def fingerprint(ws: int) -> str:
    """The first 8 characters of the stored hash: enough to see that two agents were given the same token, nothing that helps guess it."""
    return (config.get("workspace", ws, "debug_token_hash") or "")[:8]


def token_problem(token: str) -> str:
    if len(token) < MIN_TOKEN_LEN:
        return f"the token must be at least {MIN_TOKEN_LEN} characters (a random one: it is the only thing between the internet and your conversations)"
    if re.search(r"\s", token):
        return "the token cannot contain spaces or line breaks"
    if len(token) > 200:
        return "the token is too long (200 characters at most)"
    return ""


def set_token(ws: int, token: str) -> tuple[bool, str]:
    problem = token_problem(token)
    if problem:
        return False, problem
    config.set("workspace", ws, "debug_token_hash", token_hash(token))
    return True, f"token saved (fingerprint {fingerprint(ws)})"


def clear_token(ws: int) -> None:
    config.set("workspace", ws, "debug_token_hash", "")
    config.set("workspace", ws, "debug_enabled", False)


def set_enabled(ws: int, on: bool, hours: float = 0) -> tuple[bool, str]:
    if not on:
        config.set("workspace", ws, "debug_enabled", False)
        config.set("workspace", ws, "debug_until_ts", 0.0)
        return True, "debug endpoint off"
    if not config.get("workspace", ws, "debug_token_hash"):
        return False, "set a token first"
    config.set("workspace", ws, "debug_enabled", True)
    config.set("workspace", ws, "debug_until_ts", time.time() + hours * 3600 if hours and hours > 0 else 0.0)
    return True, "debug endpoint on" + (f" for {hours:g} hour(s)" if hours and hours > 0 else " until you turn it off")


def is_enabled(ws: int | None) -> bool:
    """On only if the switch is on, a token exists, and the timer (if any) has not run out. A lapsed timer switches it off for good."""
    if ws is None or not config.get("workspace", ws, "debug_enabled") or not config.get("workspace", ws, "debug_token_hash"):
        return False
    until = config.get("workspace", ws, "debug_until_ts") or 0.0
    if until and time.time() >= until:
        config.set("workspace", ws, "debug_enabled", False)
        config.set("workspace", ws, "debug_until_ts", 0.0)
        return False
    return True


def check_token(ws: int, presented: str) -> bool:
    stored = config.get("workspace", ws, "debug_token_hash") or ""
    if not stored or not presented:
        return False
    return hmac.compare_digest(token_hash(presented), stored)


def bearer(headers) -> str:
    h = (headers.get("Authorization") or "").strip()
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


# ── the access record ────────────────────────────────────────────────────
def audit(ip: str, path: str, status: int) -> None:
    print(f"debug: {status} {ip} GET {path[:120]}", flush=True)
    try:
        def w(c):
            c.execute("INSERT INTO debug_access_log(ts, ip, path, status) VALUES (?,?,?,?)", (time.time(), (ip or "")[:64], path[:300], status))
            c.execute("DELETE FROM debug_access_log WHERE id <= (SELECT MAX(id) FROM debug_access_log) - ?", (AUDIT_KEEP,))
        store.write(w)
    except Exception:  # noqa: BLE001 -- the record is a courtesy; it must never break answering
        pass


def recent_access(limit: int = 20) -> list[dict]:
    return _rows("SELECT ts, ip, path, status FROM debug_access_log ORDER BY id DESC LIMIT ?", (limit,))


# ── scrubbing ────────────────────────────────────────────────────────────
_SECRET_KEY = re.compile(
    r"(?i)(^|_|-)(secret|password|passwd|psk|api_?key|authorization|cookie|salt|credential|csrf|bearer|signature)"
    r"|(^|_)(access|refresh|bearer|session|device|pairing|invite|bridge|auth|oauth|id)_?token"
    r"|^tokens?$|token_hash|_enc$|_hash$|(^|_)key$|^totp|private")
_STR_PATTERNS = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [redacted]"),
    (re.compile(r"\b(sk|xai|pk|rk)-[A-Za-z0-9_-]{12,}"), "[redacted-key]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"), "[redacted-key]"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{12,}"), "[redacted-key]"),
    (re.compile(r"\bpaci1:[A-Za-z0-9_-]{12,}"), "paci1:[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(\.[A-Za-z0-9_-]*)?"), "[redacted-jwt]"),
    (re.compile(r"\bns=[A-Za-z0-9_-]{16,}"), "ns=[redacted]"),
    (re.compile(r"(?i)\b(token|key|secret|password|psk|api_?key)(\"?\s*[:=]\s*\"?)[A-Za-z0-9._~+/=-]{12,}"), r"\1\2[redacted]"),
    (re.compile(r"(?i)([?&](token|key|secret|api_?key|access_token|auth|sig|signature)=)[^&\s\"']+"), r"\1[redacted]"),
]


def scrub_text(s: str) -> str:
    for rx, rep in _STR_PATTERNS:
        s = rx.sub(rep, s)
    return s


def safe_url(u) -> str:
    """A URL without credentials or query string (either can carry a secret)."""
    try:
        p = urllib.parse.urlsplit(str(u or ""))
        host = p.hostname or ""
        if p.port:
            host += f":{p.port}"
        return urllib.parse.urlunsplit((p.scheme, host, p.path, "", ""))
    except ValueError:
        return "[unparseable url]"


def scrub(obj, _key: str = ""):
    """A copy of `obj` with every secret-named field redacted and every secret-looking string masked."""
    if isinstance(obj, dict):
        return {k: ("[redacted]" if isinstance(k, str) and _SECRET_KEY.search(k) and v not in (None, "", 0, False) else scrub(v, k if isinstance(k, str) else ""))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(v, _key) for v in obj]
    if isinstance(obj, str):
        return scrub_text(obj)
    return obj


# ── helpers ──────────────────────────────────────────────────────────────
def _rows(sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in store.read(lambda c: c.execute(sql, params).fetchall())]


def _one(sql: str, params: tuple = ()):
    r = store.read(lambda c: c.execute(sql, params).fetchone())
    return dict(r) if r else None


def _cut(s, n: int):
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n] + f"... [{len(s) - n} more characters]"


def _json(s):
    try:
        return json.loads(s) if s else None
    except (ValueError, TypeError):
        return None


def _safe(fn):
    """A section that breaks must still say so: this tool exists to look at things that are broken."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}


def _ws() -> int:
    return accounts.the_workspace_id()


def _int(params: dict, key: str, default: int, lo: int = 1, hi: int = MAX_ROWS) -> int:
    try:
        return max(lo, min(int(params.get(key, default)), hi))
    except (TypeError, ValueError):
        return default


def _users() -> list[dict]:
    return _rows("SELECT id, display_name, role, status, created_ts, activated_ts FROM users ORDER BY id")


def _resolve_user(spec: str) -> dict | None:
    users = _users()
    spec = (spec or "").strip()
    if not spec:
        return next((u for u in users if u["role"] == "admin"), users[0] if users else None)
    if spec.isdigit():
        return next((u for u in users if u["id"] == int(spec)), None)
    return next((u for u in users if u["display_name"].lower() == spec.lower()), None)


# ── sections ─────────────────────────────────────────────────────────────
def overview(params: dict) -> dict:
    import updates
    ws = _ws()
    chain = _rows(
        "SELECT mc.priority, m.alias, m.model_name, m.reasoning_effort, m.enabled AS model_enabled, p.type AS provider_type, p.label AS provider_label, "
        "p.status AS provider_status, p.enabled AS provider_enabled FROM model_chain mc JOIN models m ON m.id=mc.model_id "
        "LEFT JOIN providers p ON p.id=m.provider_id WHERE mc.workspace_id=? ORDER BY mc.priority", (ws,))
    msgs = _one("SELECT COUNT(*) AS n, MAX(ts) AS last_ts FROM messages") or {}
    day = _one("SELECT COUNT(*) AS n FROM messages WHERE ts > ?", (time.time() - 86400,)) or {}
    try:
        usage = shutil.disk_usage(store.DATA_DIR)
        disk = {"free_mb": usage.free // 2**20, "total_mb": usage.total // 2**20}
    except OSError:
        disk = {}
    db_file = getattr(store, "DB_PATH", None)
    return {
        "assistant_name": config.get("workspace", ws, "assistant_name"), "workspace": (_one("SELECT name FROM workspaces WHERE id=?", (ws,)) or {}).get("name"),
        "version": updates.current_version(), "started_ts": STARTED_TS, "uptime_s": round(time.time() - STARTED_TS), "now": time.time(),
        "python": sys.version.split()[0], "platform": platform.platform(), "pid": os.getpid(),
        "users": _users(), "model_chain": chain,
        "providers": _rows("SELECT id, type, label, status, enabled, token_expires_ts, created_ts FROM providers ORDER BY id"),
        "messages": {"total": msgs.get("n"), "last_ts": msgs.get("last_ts"), "last_24h": day.get("n")},
        "recent_log_errors": len(logbuf.recent(MAX_ROWS, level="error", since_ts=time.time() - 3600)),
        "disk": disk, "db_bytes": os.path.getsize(db_file) if db_file and os.path.exists(db_file) else None,
        "env_set": sorted(k for k in os.environ if k.startswith("NORI_")),
    }


def health(params: dict) -> dict:
    import app_fetch
    import integration_health
    import sandbox
    import scheduler
    import updates
    ws = _ws()
    out: dict = {}
    out["integrations"] = _safe(lambda: integration_health.get_all())
    out["peers_paci"] = _safe(lambda: _rows(
        "SELECT p.id, p.name, p.url, p.enabled, h.last_status, h.last_checked_ts, h.last_detail, h.status_changed_ts "
        "FROM peers p LEFT JOIN paci_health h ON h.peer_id=p.id ORDER BY p.id"))
    for r in out["peers_paci"] if isinstance(out["peers_paci"], list) else []:
        r["url"] = safe_url(r["url"])
    out["devices"] = _safe(lambda: _rows(
        "SELECT id, user_id, label, kind, created_ts, last_seen_ts, revoked_ts, paused, caps, state, app_version FROM bridge_tokens ORDER BY id"))
    out["connected_accounts"] = _safe(lambda: _rows("SELECT id, user_id, provider, status, token_expires_ts, connected_ts FROM connected_accounts ORDER BY id"))
    out["providers"] = _safe(lambda: _rows("SELECT id, type, label, status, enabled, token_expires_ts FROM providers ORDER BY id"))
    out["scheduler"] = _safe(lambda: dict(getattr(scheduler, "LAST_CYCLE", {})))
    out["threads"] = [{"name": t.name, "alive": t.is_alive(), "daemon": t.daemon} for t in threading.enumerate()]
    out["updates"] = _safe(lambda: {k: v for k, v in updates.status(ws).items() if k != "release"})
    out["phone_app_fetch"] = _safe(lambda: app_fetch.status(ws))
    out["sandbox"] = _safe(lambda: sandbox.status())
    out["jobs_running"] = _safe(lambda: _rows("SELECT id, user_id, sub_agent_id, status, started_ts, timeout_s FROM jobs WHERE status IN ('running','queued') ORDER BY id"))
    out["mcp_servers"] = _safe(lambda: [dict(r, url=safe_url(r["url"])) for r in _rows(
        "SELECT id, scope, name, url, auth_type, enabled FROM mcp_servers ORDER BY id")])
    return out


def errors(params: dict) -> dict:
    import diagnostics
    import jobs
    limit = _int(params, "limit", 100, hi=300)
    out: dict = {}
    out["events"] = _safe(lambda: diagnostics.events(limit))
    out["log_errors"] = logbuf.recent(limit, level="error")
    out["log_error_counts"] = {
        "screening_failed": len(logbuf.recent(MAX_ROWS * 8, contains="SCREENING FAILED")),
        "reply_cut_off": len(logbuf.recent(MAX_ROWS * 8, contains="REPLY CUT OFF")),
        "tracebacks": len(logbuf.recent(MAX_ROWS * 8, contains="Traceback")),
    }
    out["failed_tool_calls"] = _safe(lambda: [
        {"id": r["id"], "user_id": r["user_id"], "ts": r["ts"], "text": r["content"], "meta": _json(r["meta"])}
        for r in _rows("SELECT id, user_id, ts, content, meta FROM messages WHERE kind='tool' AND meta LIKE '%\"failed\"%' ORDER BY id DESC LIMIT ?", (limit,))])
    out["job_problems"] = _safe(lambda: jobs.recent_problems(30))
    out["peer_message_problems"] = _safe(lambda: _rows(
        "SELECT pm.id, pm.peer_id, p.name AS peer, pm.direction, pm.type, pm.status, pm.attempts, pm.last_error, pm.ts FROM peer_messages pm "
        "LEFT JOIN peers p ON p.id=pm.peer_id WHERE pm.last_error IS NOT NULL OR pm.status IN ('failed','expired') ORDER BY pm.id DESC LIMIT ?", (limit,)))
    out["peer_model_failures"] = _safe(lambda: _rows("SELECT ts, peer_id, error, reason FROM peer_model_failure_log ORDER BY id DESC LIMIT 30"))
    out["peer_turn_limit_hits"] = _safe(lambda: _rows("SELECT ts, peer_id, rounds, reason FROM peer_turn_limit_log ORDER BY id DESC LIMIT 30"))
    out["schedule_failures"] = _safe(lambda: _rows(
        "SELECT sr.ts, s.name, sr.status, sr.error FROM schedule_runs sr JOIN schedules s ON s.id=sr.schedule_id WHERE sr.error IS NOT NULL AND sr.error != '' "
        "ORDER BY sr.id DESC LIMIT 30"))
    out["image_failures"] = _safe(lambda: _rows("SELECT ts, kind, purpose, model, error FROM media_log WHERE ok=0 ORDER BY id DESC LIMIT 20"))
    return out


def logs(params: dict) -> dict:
    lines = logbuf.recent(_int(params, "lines", 300, hi=logbuf.MAX_LINES), contains=params.get("grep", ""), level=params.get("level", ""),
                          stream=params.get("stream", ""), since_ts=float(params.get("since", 0) or 0))
    return {"count": len(lines), "capacity": logbuf.MAX_LINES, "note": "in-memory ring for this run only; lost on restart", "lines": lines}


def conversation(params: dict) -> dict:
    user = _resolve_user(params.get("user", ""))
    if not user:
        return {"error": "no such user", "users": [{"id": u["id"], "name": u["display_name"]} for u in _users()]}
    limit = _int(params, "limit", 60, hi=300)
    kinds = [k for k in re.split(r"[,\s]+", params.get("kinds", "")) if re.fullmatch(r"[a-z_]+", k or "")]
    sql = "SELECT id, ts, role, kind, emotion, content, meta, pruned FROM messages WHERE user_id=?"
    args: list = [user["id"]]
    if params.get("before", "").isdigit():
        sql += " AND id < ?"
        args.append(int(params["before"]))
    if params.get("after", "").isdigit():
        sql += " AND id > ?"
        args.append(int(params["after"]))
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        args += kinds
    if params.get("grep"):
        sql += " AND content LIKE ?"
        args.append(f"%{params['grep']}%")
    rows = _rows(sql + " ORDER BY id DESC LIMIT ?", tuple(args + [limit]))
    rows.reverse()
    msgs = [{"id": r["id"], "ts": r["ts"], "role": r["role"], "kind": r["kind"], "emotion": r["emotion"], "pruned": bool(r["pruned"]),
             "content": _cut(r["content"], 20000), "meta": _json(r["meta"])} for r in rows]
    segs = _rows("SELECT id, lo_id, hi_id, lo_ts, hi_ts, generated_ts, text FROM conversation_segments WHERE user_id=? ORDER BY id DESC LIMIT 5", (user["id"],))
    return {"user": {"id": user["id"], "name": user["display_name"]}, "count": len(msgs), "oldest_id": msgs[0]["id"] if msgs else None,
            "newest_id": msgs[-1]["id"] if msgs else None, "page_back_with": "before=<oldest_id>", "messages": msgs,
            "summaries": [dict(s, text=_cut(s["text"], 3000)) for s in segs]}


def turns(params: dict) -> dict:
    """Recent replies with how each was produced: model, provider, fallbacks tried, latency, tokens, cost, tool calls, whether it was cut off."""
    user = _resolve_user(params.get("user", ""))
    if not user:
        return {"error": "no such user"}
    limit = _int(params, "limit", 30, hi=200)
    rows = _rows("SELECT id, ts, kind, emotion, content, meta FROM messages WHERE user_id=? AND role='assistant' AND kind IN ('chat','proactive','ping','reminder') "
                 "ORDER BY id DESC LIMIT ?", (user["id"], limit))
    out = []
    for r in rows:
        meta = _json(r["meta"]) or {}
        out.append({"id": r["id"], "ts": r["ts"], "kind": r["kind"], "emotion": r["emotion"], "chars": len(r["content"] or ""),
                    "preview": _cut(r["content"], 240), "meta": meta})
    return {"user": {"id": user["id"], "name": user["display_name"]}, "turns": out}


def settings(params: dict) -> dict:
    ws = _ws()
    spec = config.readable_spec()
    wsv, per_user = {}, {}
    for key, (default, _typ, scope, _secret, label) in spec.items():
        if scope == "workspace":
            v = config.get("workspace", ws, key)
            wsv[key] = {"value": v, "default": default, "changed": v != default, "label": label}
    for u in _users():
        vals = {}
        for key, (default, _typ, scope, _secret, label) in spec.items():
            if scope == "user":
                v = config.get("user", u["id"], key)
                vals[key] = {"value": v, "default": default, "changed": v != default, "label": label}
        per_user[u["display_name"]] = vals
    changed = {"workspace": [k for k, v in wsv.items() if v["changed"]],
               "users": {n: [k for k, v in vals.items() if v["changed"]] for n, vals in per_user.items()}}
    allowed_env = ("NORI_MODEL", "NORI_MAX_TOKENS", "NORI_TEMPERATURE", "NORI_API_TIMEOUT_S", "NORI_MAX_TOOL_ROUNDS", "NORI_REASONING_EFFORT", "NORI_UPDATE_REPO",
                   "NORI_BIND_HOST", "NORI_PORT", "NORI_NO_LOGFILE", "NORI_LIVE")
    return {"changed_from_default": changed, "workspace": wsv, "users": per_user,
            "environment": {k: os.environ[k] for k in allowed_env if k in os.environ},
            "environment_set_but_not_shown": sorted(k for k in os.environ if k.startswith("NORI_") and k not in allowed_env)}


def peers(params: dict) -> dict:
    limit = _int(params, "messages", 20, hi=100)
    out = []
    for p in _rows("SELECT id, name, purpose, scope, url, enabled, trust_level, screening_enabled, receipt_granularity, turn_limit, cooldown_minutes, daily_cap, "
                   "expiry_hours, remote_turn_limit, remote_cooldown_minutes, remote_daily_cap, last_hello_ts, last_peer_check_ts, last_forced_checkin_ts, "
                   "self_agent_id, self_agent_name, created_ts, invite_address FROM peers ORDER BY id"):
        health_row = _one("SELECT last_status, last_checked_ts, last_detail, status_changed_ts FROM paci_health WHERE peer_id=?", (p["id"],))
        msgs = []
        for m in _rows("SELECT id, direction, type, status, attempts, last_error, ts, conversation_id, body_json, presented_peer_ts, presented_user_ts "
                       "FROM peer_messages WHERE peer_id=? ORDER BY id DESC LIMIT ?", (p["id"], limit)):
            body = _json(m.pop("body_json")) or {}
            m["screening"] = {k: body.get(k) for k in ("category", "priority", "suspicious", "screening_failed", "summary", "suggested_action") if k in body}
            m["content"] = _cut(body.get("content") or body.get("text") or body.get("summary") or "", 1500)
            msgs.append(m)
        p["url"] = safe_url(p["url"])
        p["invite_address"] = safe_url(p["invite_address"]) if p["invite_address"] else None
        out.append({**p, "paci_health": health_row, "recent_messages": msgs})
    return {"peers": out,
            "compulsion_log": _safe(lambda: _rows("SELECT ts, peer_id, decision, detail FROM peer_compulsion_log ORDER BY id DESC LIMIT 30")),
            "pending_actions": _safe(lambda: _rows("SELECT * FROM peer_pending_actions ORDER BY id DESC LIMIT 20"))}


def memory(params: dict) -> dict:
    limit = _int(params, "limit", 50, hi=300)
    return {"counts": _rows("SELECT user_id, type, COUNT(*) AS n, SUM(pinned) AS pinned FROM memory GROUP BY user_id, type ORDER BY user_id, type"),
            "recent": [dict(r, value=_cut(r["value"], 1000)) for r in _rows(
                "SELECT id, user_id, scope, type, value, tags, source, pinned, safety_tier, created_ts, updated_ts, last_used_ts FROM memory "
                "ORDER BY COALESCE(updated_ts, created_ts) DESC LIMIT ?", (limit,))],
            "events": _rows("SELECT ts, memory_id, user_id, action, actor, type, note FROM memory_events ORDER BY id DESC LIMIT 40"),
            "backend": config.get("workspace", _ws(), "memory_backend") if "memory_backend" in config.spec() else None}


def jobs_section(params: dict) -> dict:
    limit = _int(params, "limit", 30, hi=200)
    return {"jobs": [dict(r, task=_cut(r["task"], 400), result=_cut(r["result"], 800), error=_cut(r["error"], 500), trace=_cut(r["trace"], 2000))
                     for r in _rows("SELECT id, user_id, sub_agent_id, task, status, result, error, created_ts, started_ts, finished_ts, timeout_s, cost_usd, "
                                    "prompt_tokens, completion_tokens, tool_calls_used, tool_bytes_used, trace FROM jobs ORDER BY id DESC LIMIT ?", (limit,))],
            "sub_agents": _rows("SELECT id, label, model_id, enabled, tool_call_limit, tool_byte_limit, file_write, web_access, write_folder, raw_file_access, "
                                "timeout_s, code_exec FROM sub_agents ORDER BY id")}


def schedules_section(params: dict) -> dict:
    return {"schedules": [dict(r, instruction=_cut(r["instruction"], 500)) for r in _rows(
                "SELECT id, user_id, name, instruction, required_tool, deliver_to, schedule_type, interval_min, time_hour, time_minute, enabled, last_run_ts, "
                "next_run_ts, last_status, last_error FROM schedules ORDER BY id")],
            "recent_runs": _rows("SELECT sr.id, sr.schedule_id, s.name, sr.ts, sr.status, sr.error FROM schedule_runs sr LEFT JOIN schedules s ON s.id=sr.schedule_id "
                                 "ORDER BY sr.id DESC LIMIT 40")}


def pings(params: dict) -> dict:
    import scheduler
    return {"signals": scheduler.signal_registry(),
            "state": _rows("SELECT user_id, signal_key, dedup_token, last_checked_ts, last_fired_ts FROM ping_signal_state ORDER BY user_id, signal_key"),
            "spend": _safe(lambda: _rows("SELECT * FROM ping_signal_spend ORDER BY rowid DESC LIMIT 20")),
            "recent_proactive_messages": [dict(r, content=_cut(r["content"], 400), meta=_json(r["meta"])) for r in _rows(
                "SELECT id, user_id, ts, kind, content, meta FROM messages WHERE kind IN ('proactive','ping') ORDER BY id DESC LIMIT 20")]}


def tools_section(params: dict) -> dict:
    import tools
    rows = []
    for name, t in sorted(tools._REGISTRY.items()):
        rows.append({"name": name, "min_role": t.min_role, "risk_tier": t.risk_tier, "data_scope": t.data_scope, "consequential": t.consequential,
                     "always_enabled": t.enabled if isinstance(t.enabled, bool) else "per-session"})
    return {"count": len(rows), "tools": rows}


def infra_section(params: dict) -> dict:
    """Infrastructure tools (infra.py): which services are on, which credential fields are SET (never their values), the change policies, what is waiting for
    approval, and the audit log."""
    import infra
    import infra_proxmox  # noqa: F401
    ws = _ws()
    return {"services": {k: {"label": s["label"], "switch_on": bool(config.get("workspace", ws, infra.switch_key(k))), "enabled": infra.enabled(k, ws),
                             "fields_set": [{"field": f["key"], "set": f["set"]} for f in infra.credential_status(ws, k)]} for k, s in infra.SERVICES.items()},
            "policies": infra.policies(ws), "pending": infra.pending(ws),
            "recent_pending_requests": _rows("SELECT id, user_id, service, action, summary, status, created_ts, resolved_ts, error FROM infra_pending ORDER BY id DESC LIMIT 20"),
            "log": infra.recent_log(int(params.get("limit", 80) or 80))}


def db(params: dict) -> dict:
    names = [r["name"] for r in _rows("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {}
    for n in names:
        if re.fullmatch(r"[A-Za-z0-9_]+", n):
            counts[n] = _safe(lambda n=n: _one(f"SELECT COUNT(*) AS n FROM {n}")["n"])
    return {"tables": counts}


SECTIONS = {
    "overview": (overview, "version, uptime, users, model chain, providers, message counts, disk"),
    "health": (health, "integrations, peer reachability, devices, accounts, scheduler, threads, updates, sandbox, MCP, running jobs"),
    "errors": (errors, "events, log errors, failed tool calls, job/peer/schedule/image failures  (limit=)"),
    "logs": (logs, "the in-memory log ring  (lines=, grep=, level=error|info, stream=out|err, since=<epoch>)"),
    "conversation": (conversation, "a user's messages with metadata  (user=<id|name>, limit=, before=, after=, kinds=chat,tool,..., grep=)"),
    "turns": (turns, "recent replies: model/provider/fallbacks/latency/tokens/cost/tool calls  (user=, limit=)"),
    "settings": (settings, "every non-secret setting, workspace and per user, with what differs from default; environment"),
    "peers": (peers, "PACI peers: config, health, recent messages with screening results  (messages=)"),
    "memory": (memory, "memory counts, recent items, events  (limit=)"),
    "jobs": (jobs_section, "sub-agent jobs and the roster  (limit=)"),
    "schedules": (schedules_section, "schedules and recent runs"),
    "pings": (pings, "proactive ping signals, their state, recent unprompted messages"),
    "tools": (tools_section, "the tool registry"),
    "infra": (infra_section, "infrastructure tools: which are on, credential fields set (never values), change policies, pending approvals, audit log"),
    "db": (db, "row counts per table"),
}


def handle(section: str, params: dict) -> tuple[int, dict]:
    """(status, body) for GET /debug/<section>. The body is scrubbed and size-capped."""
    if section in ("", "index"):
        body = {"ok": True, "sections": {k: v[1] for k, v in SECTIONS.items()}, "usage": "GET /debug/<section>?param=value  with  Authorization: Bearer <token>"}
        return 200, scrub(body)
    entry = SECTIONS.get(section)
    if entry is None:
        return 404, {"ok": False, "error": "no such section", "sections": sorted(SECTIONS)}
    ws = _ws()
    data = _safe(lambda: entry[0](params))
    body = scrub({"ok": True, "section": section, "generated_ts": time.time(), "assistant": config.get("workspace", ws, "assistant_name"), "data": data})
    raw = json.dumps(body, default=str)
    if len(raw) > MAX_RESPONSE_BYTES:
        return 413, {"ok": False, "error": f"response is {len(raw)} bytes (limit {MAX_RESPONSE_BYTES}): ask for less (limit=, lines=, messages=)"}
    return 200, body

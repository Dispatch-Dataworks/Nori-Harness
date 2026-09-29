# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Workspaces, users, sessions, invites, auth — the only module that runs
raw SQL against those tables (see store.py's docstring for why that's the
rule, not a convention to remember). Retyped from a sibling application's auth pattern,
not shared code.

Role is exactly two values everywhere in this app: 'admin' or 'member'. A
workspace is the household; every user belongs to exactly one. The first
account ever created on a fresh instance becomes that workspace's admin
automatically (`bootstrap_admin`) — there's no one else yet to invite it.

This module doesn't know about HTTP sessions-as-cookies or role-based route
gating — it hands back/accepts plain dicts and tokens; server.py owns the
cookie/CSRF/routing layer on top of it.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

import store

SESSION_TTL_S = 24 * 3600
INVITE_TTL_S = 7 * 24 * 3600
# Interactive-login scrypt cost -- tune if this ever measurably lags; not a
# security knob anyone should need to touch day to day.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1

MAX_FAILS, FAIL_WINDOW = 5, 900
_LOGIN_FAILS: dict[str, list[float]] = {}


# ── passwords ────────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def _hash_looks_valid(stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = (stored or "").split("$")
    except ValueError:
        return False
    if scheme != "scrypt" or not (n.isdigit() and r.isdigit() and p.isdigit()):
        return False
    try:
        bytes.fromhex(salt_hex); bytes.fromhex(hash_hex)
    except ValueError:
        return False
    return len(salt_hex) >= 16 and len(hash_hex) >= 16


def verify_password(password: str, stored: str) -> bool:
    if not _hash_looks_valid(stored):
        return False
    _, n, r, p, salt_hex, hash_hex = stored.split("$")
    try:
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2)
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def rate_limited(ip: str) -> bool:
    now = time.time()
    _LOGIN_FAILS[ip] = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < FAIL_WINDOW]
    return len(_LOGIN_FAILS[ip]) >= MAX_FAILS


def record_fail(ip: str) -> None:
    _LOGIN_FAILS.setdefault(ip, []).append(time.time())


# ── workspace / first-run ────────────────────────────────────────────────
def any_users_exist() -> bool:
    return store.read(lambda c: c.execute("SELECT 1 FROM users LIMIT 1").fetchone()) is not None


def the_workspace_id() -> int | None:
    """bootstrap_admin() is the ONLY inserter into `workspaces` (see its own
    docstring: "First-run only... creates the instance's one workspace") --
    a Nori install has exactly one, for its whole lifetime, ever. For the
    rare genuinely-public, no-session surface (2026-09-25: the PWA manifest,
    served before login) that still needs a workspace-scoped setting like
    assistant_name -- never a stand-in for a real session's own
    sess['workspace_id']. None on a fresh, not-yet-set-up install."""
    r = store.read(lambda c: c.execute("SELECT id FROM workspaces ORDER BY id LIMIT 1").fetchone())
    return r["id"] if r else None


def bootstrap_admin(display_name: str, password: str) -> dict | None:
    """First-run only. Creates the instance's one workspace and its first
    user, role='admin', active immediately — no invite step for account #1,
    since there's no one else yet to issue one. Re-checks any_users_exist()
    under the write lock so a race between two first-run requests can't
    produce two admins; returns None if it lost that race."""
    def _w(c):
        if c.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
            return None
        now = time.time()
        ws_id = c.execute(
            "INSERT INTO workspaces(created_ts, name) VALUES (?, 'My household')", (now,)).lastrowid
        uid = c.execute(
            "INSERT INTO users(workspace_id, role, display_name, password_hash, status, "
            "created_ts, activated_ts) VALUES (?, 'admin', ?, ?, 'active', ?, ?)",
            (ws_id, display_name, hash_password(password), now, now)).lastrowid
        return {"id": uid, "workspace_id": ws_id, "role": "admin", "display_name": display_name}
    return store.write(_w)


# ── invites ──────────────────────────────────────────────────────────────
def create_invite(workspace_id: int, display_name: str, role: str, created_by: int) -> tuple[int, str]:
    """Admin-only in practice — enforced by the caller (server.py checks the
    session's role), not here; this module doesn't know what a session is.
    Returns (user_id, raw_token): the raw token is shown to the admin
    exactly once to hand to the invitee out-of-band. Only its hash is ever
    stored, so admin never sets — or even briefly holds — the new user's
    actual password."""
    if role not in ("admin", "member"):
        raise ValueError(f"invalid role: {role!r}")
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    now = time.time()
    uid = store.write(lambda c: c.execute(
        "INSERT INTO users(workspace_id, role, display_name, status, created_ts, created_by, "
        "invite_token_hash, invite_expires_ts) VALUES (?,?,?,'pending_invite',?,?,?,?)",
        (workspace_id, role, display_name, now, created_by, token_hash, now + INVITE_TTL_S)
    ).lastrowid)
    return uid, raw


def get_pending_invite(raw_token: str) -> dict | None:
    if not raw_token:
        return None
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    r = store.read(lambda c: c.execute(
        "SELECT * FROM users WHERE invite_token_hash=? AND status='pending_invite'",
        (token_hash,)).fetchone())
    if not r or r["invite_expires_ts"] < time.time():
        return None
    return dict(r)


def accept_invite(raw_token: str, password: str) -> dict | None:
    inv = get_pending_invite(raw_token)
    if inv is None:
        return None
    now = time.time()
    store.write(lambda c: c.execute(
        "UPDATE users SET password_hash=?, status='active', activated_ts=?, "
        "invite_token_hash=NULL, invite_expires_ts=NULL WHERE id=?",
        (hash_password(password), now, inv["id"])))
    return get_user(inv["id"])


# ── users ────────────────────────────────────────────────────────────────
def get_user(user_id: int) -> dict | None:
    r = store.read(lambda c: c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
    return dict(r) if r else None


def get_user_by_name(display_name: str) -> dict | None:
    r = store.read(lambda c: c.execute(
        "SELECT * FROM users WHERE display_name=? AND status='active'", (display_name,)).fetchone())
    return dict(r) if r else None


def authenticate(display_name: str, password: str) -> dict | None:
    u = get_user_by_name(display_name)
    if u is None or not u["password_hash"] or not verify_password(password, u["password_hash"]):
        return None
    return u


def list_users(workspace_id: int) -> list[dict]:
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT id, role, display_name, status, created_ts, activated_ts, deactivated_ts "
        "FROM users WHERE workspace_id=? ORDER BY created_ts", (workspace_id,)).fetchall())]


def all_active_users() -> list[dict]:
    """Across every workspace -- used by the proactive scheduler, which
    runs once for the whole instance, not per-workspace."""
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT id, workspace_id, role, display_name FROM users WHERE status='active'").fetchall())]


def get_workspace(workspace_id: int) -> dict | None:
    r = store.read(lambda c: c.execute("SELECT * FROM workspaces WHERE id=?", (workspace_id,)).fetchone())
    return dict(r) if r else None


def rename_workspace(workspace_id: int, name: str) -> None:
    name = (name or "").strip() or "My household"
    store.write(lambda c: c.execute(
        "UPDATE workspaces SET name=? WHERE id=?", (name, workspace_id)))


def deactivate_user(user_id: int) -> None:
    """Disables login and kills every active session for this user. Doesn't
    touch their data — deactivation is the first step of a three-step
    lifecycle (deactivate, then archive, then purge); archive and purge are
    later, separate, typed-confirmation steps, not implied by this one."""
    now = time.time()
    def _w(c):
        c.execute("UPDATE users SET status='deactivated', deactivated_ts=? WHERE id=?", (now, user_id))
        c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    store.write(_w)


def reactivate_user(user_id: int) -> None:
    """Re-enables login only. Does not touch or reset their password (still
    only ever known to them) and grants admin no new visibility into their
    data — the same admin-can't-see-in principle holds through the whole
    lifecycle, not just steady state."""
    store.write(lambda c: c.execute(
        "UPDATE users SET status='active', deactivated_ts=NULL WHERE id=?", (user_id,)))


# ── sessions ─────────────────────────────────────────────────────────────
def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_session(user: dict, ip: str) -> str:
    """Returns the raw token (goes in the cookie). Only its hash is stored —
    someone with read access to the DB can't replay a session directly from
    what's at rest there."""
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    now = time.time()
    store.write(lambda c: c.execute(
        "INSERT INTO sessions(token_hash, user_id, workspace_id, role, created_ts, expires_ts, csrf, client_ip) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (_token_hash(raw), user["id"], user["workspace_id"], user["role"], now, now + SESSION_TTL_S, csrf, ip)))
    return raw


def get_session(raw_token: str) -> dict | None:
    if not raw_token:
        return None
    r = store.read(lambda c: c.execute(
        "SELECT * FROM sessions WHERE token_hash=?", (_token_hash(raw_token),)).fetchone())
    if not r or r["expires_ts"] < time.time():
        return None
    return dict(r)


def delete_session(raw_token: str) -> None:
    store.write(lambda c: c.execute(
        "DELETE FROM sessions WHERE token_hash=?", (_token_hash(raw_token),)))

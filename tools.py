# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The tool registry and its single enforcement point: every tool declares who can call it
(`min_role`) and what it's scoped to (`data_scope`, `risk_tier`); dispatch()
is the ONLY place those are checked, and the ONLY place an implementation
ever learns which user/workspace it's running for.

No tool schema defines a user_id/workspace_id parameter -- ever -- so there
is nothing for the model to pass that could override identity in the first
place. Every implementation receives `session` (the caller's own, already-
authenticated session dict) and reads ids from THAT, never from arguments.
dispatch() also strips any user_id/workspace_id key out of the model's
arguments before they reach an implementation, as a second line of defense
that holds even if a schema ever slipped up and defined one.

Tier D (system: manage users, approve tools, manage the sub-agent roster,
read audit logs) is a hard floor, not a default -- enforced at registration
time below (raises immediately, at import, not discovered at call time).

`data_scope`/`risk_tier` are metadata, not independent runtime gates --
every native tool is self-scoping by construction (its own impl reads
session["user_id"]/["workspace_id"] to know what to touch), so nothing
here has ever needed to check them separately. `owner_check` (optional,
see Tool.__init__) is the one real exception: for a tool whose resource
is fixed at registration time rather than derived from the calling
session -- an MCP tool bound to one specific connection, which itself
belongs to one specific user or workspace -- dispatch() and
active_schemas() both apply it as an ADDITIONAL gate on top of min_role.
"""
from __future__ import annotations

import os
import threading
import time

import accounts
import timing

_VALID_ROLES = ("admin", "member")
_VALID_SCOPES = ("self", "workspace", "system")
_VALID_TIERS = ("A", "B", "C", "D")

RATE_LIMIT_CALLS = int(os.environ.get("NORI_TOOL_RATE_LIMIT", "30"))
RATE_LIMIT_WINDOW_S = int(os.environ.get("NORI_TOOL_RATE_WINDOW_S", "60"))

_rate_lock = threading.Lock()
_rate_calls: dict[tuple[int, str], list[float]] = {}


class Tool:
    def __init__(self, name, schema, impl, *, min_role="member", data_scope="self",
                 risk_tier="A", enabled=True, owner_check=None, consequential=False):
        if min_role not in _VALID_ROLES:
            raise ValueError(f"{name}: invalid min_role {min_role!r}")
        if data_scope not in _VALID_SCOPES:
            raise ValueError(f"{name}: invalid data_scope {data_scope!r}")
        if risk_tier not in _VALID_TIERS:
            raise ValueError(f"{name}: invalid risk_tier {risk_tier!r}")
        if risk_tier == "D" and min_role != "admin":
            raise ValueError(f"{name}: risk tier D is a hard floor -- min_role must "
                             f"be 'admin', got {min_role!r}")
        self.name = name
        self.schema = schema
        self.impl = impl
        self.min_role = min_role
        self.data_scope = data_scope
        self.risk_tier = risk_tier
        # bool (the common case -- a tool that's just always on) OR a
        # callable(session) -> bool, checked fresh on every access (2026-09-25,
        # emotion.py: a real settings toggle, not a fixed-at-import flag) --
        # same shape as owner_check just below, on purpose, rather than a
        # second mechanism: a session-scoped live check already has a home.
        self.enabled = enabled
        # Every native tool is self-scoping by construction: its own impl
        # reads session["user_id"]/["workspace_id"] to decide what data to
        # touch, so "data_scope=self" has always been true in practice
        # without dispatch() needing to check anything -- data_scope/
        # risk_tier are metadata here, not independent gates. That
        # assumption breaks for a tool whose underlying resource is fixed
        # at REGISTRATION time rather than derived from the calling
        # session (an MCP tool bound to one specific server connection,
        # which itself belongs to one specific user or workspace) --
        # discovered as a real gap, not a hypothetical one, when a member-
        # wide MCP widening was about to ship without it. owner_check(
        # session) -> bool, when given, is an ADDITIONAL gate dispatch()
        # and active_schemas() both apply on top of min_role -- returning
        # False makes the tool invisible in that session's schema list
        # (not just blocked if called), consistent with how an admin-only
        # tool already disappears for a member rather than merely
        # rejecting the call.
        self.owner_check = owner_check
        # Topic-triggered memory activation's point-6 pre-action hook
        # (2026-09-17, memory.py) -- a DELIBERATE, per-tool flag, set only
        # at registration, never inferred from risk_tier/data_scope: those
        # already meant something else (who can call it, what it touches),
        # and overloading them would make "should this re-check memory
        # first" an accidental side effect of an unrelated decision. Only
        # tools matching the operator's own named categories (changing
        # integrations/permissions, enabling/disabling services, smart-
        # home control, sending messages to other people, modifying
        # household/account settings) are marked True -- see chat.py's
        # tool loop and nori/docs/memory.md for which ones and why.
        self.consequential = consequential


_REGISTRY: dict[str, Tool] = {}


def _is_enabled(t: Tool, session: dict) -> bool:
    return t.enabled(session) if callable(t.enabled) else t.enabled


def register(tool: Tool) -> None:
    _REGISTRY[tool.name] = tool


def unregister(name: str) -> None:
    """The other half of dynamic (MCP) registration -- native tools never
    need this (they're registered once at import and live for the
    process's whole lifetime), but an MCP server's tool can legitimately
    need to stop being callable without a restart: the server connection
    got disabled, a tool disappeared from the server's own list, or an
    admin re-locked a previously-widened grant. A no-op if the name was
    never registered."""
    _REGISTRY.pop(name, None)


def _rate_limited(user_id: int, name: str) -> bool:
    key = (user_id, name)
    now = time.time()
    with _rate_lock:
        calls = [t for t in _rate_calls.get(key, []) if now - t < RATE_LIMIT_WINDOW_S]
        over = len(calls) >= RATE_LIMIT_CALLS
        calls.append(now)
        _rate_calls[key] = calls
        return over


def is_consequential(name: str) -> bool:
    """See Tool.consequential's own docstring -- False for an unknown or
    unregistered name, same fail-quiet-not-fail-loud posture schema_for()
    already uses for a stale name."""
    t = _REGISTRY.get(name)
    return bool(t and t.consequential)


def schema_for(name: str) -> dict | None:
    """One tool's own schema, by name -- for a caller that needs to hand a
    deliberately narrow, hardcoded allowlist to a model (jobs.py's
    sub-agent tool round: read-only working-folder access ONLY, never
    "whatever this session could call" the way active_schemas() gives a
    real turn). None if the name isn't registered, so a stale allowlist
    entry silently omits rather than crashes."""
    t = _REGISTRY.get(name)
    return t.schema if t is not None else None


def active_schemas(session: dict) -> list[dict]:
    """Schemas the calling session may actually see -- a member's list never
    includes an admin-only tool, and (see Tool.owner_check) a session that
    doesn't own a particular MCP connection never sees that connection's
    tools either. Not just blocked if called; genuinely absent, so the
    model is never told it can do something it can't."""
    role = session["role"]
    return [t.schema for t in _REGISTRY.values()
            if _is_enabled(t, session) and (role == "admin" or t.min_role != "admin")
            and (t.owner_check is None or t.owner_check(session))]


def dispatch(name: str, args: dict, session: dict, *, timing_turn: "timing.Turn | None" = None,
            **timing_extra) -> dict:
    """The one enforcement point -- every tool call in the app goes through
    this. Never raises; a rejection is a result the model can see and react
    to, not a crash.

    Timing (2026-09-13) lives HERE, not at each call site, on the same
    reasoning as putting it in ingest.summarize_untrusted(): this is
    already the ONE place every tool call in the app passes through (see
    the module docstring), so a tool call is timed regardless of which
    tool it is or who dispatches it -- including a tool registered after
    this was written, with no code change needed anywhere else. A caller
    with a live Turn (chat.run()'s own loop) passes it through for
    round-correlated breakdown inside that turn's own TIMING line;
    anything else (a proactive/peer dispatch closure that doesn't bother,
    a future caller nobody's written yet) still gets its own independent
    line via timing.standalone() -- covered either way, at the cost of
    one cheap check when no turn was passed and genuinely nothing when
    the setting's off."""
    t = _REGISTRY.get(name)
    if t is None or not _is_enabled(t, session):
        return {"error": f"no such tool: {name}"}
    if session["role"] != "admin" and t.min_role == "admin":
        return {"error": "not permitted for this account"}
    if t.owner_check is not None and not t.owner_check(session):
        # Same message as the role rejection above, deliberately -- a
        # member correctly refused a tool they don't own should not be
        # able to distinguish "wrong role" from "not your connection" by
        # the wording alone.
        return {"error": "not permitted for this account"}
    if _rate_limited(session["user_id"], name):
        return {"error": f"rate limit reached for {name} -- try again shortly"}
    safe_args = {k: v for k, v in (args or {}).items() if k not in ("user_id", "workspace_id")}
    stage = (timing_turn.stage("tool_dispatch", tool=name, **timing_extra) if timing_turn is not None
            else timing.standalone("tool_dispatch", tool=name, **timing_extra))
    try:
        with stage:
            return t.impl(session, **safe_args)
    except TypeError as exc:
        return {"error": f"bad arguments: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"tool failed: {exc}"}


# ── built-in tools ───────────────────────────────────────────────────────
# Phase 4's own proof that the framework works end to end -- real
# subsystems (memory, email/calendar once those connectors exist) register
# the same way, through the same dispatch(), with no special-casing.
def _get_my_profile(session: dict) -> dict:
    user = accounts.get_user(session["user_id"])
    return {"display_name": user["display_name"], "role": user["role"]}


register(Tool(
    "get_my_profile",
    {"type": "function", "function": {
        "name": "get_my_profile",
        "description": "Look up the current user's own name and role on this instance.",
        "parameters": {"type": "object", "properties": {}}}},
    _get_my_profile, min_role="member", data_scope="self", risk_tier="A"))


def _list_household(session: dict) -> dict:
    return {"users": accounts.list_users(session["workspace_id"])}


register(Tool(
    "list_household",
    {"type": "function", "function": {
        "name": "list_household",
        "description": "List every account on this instance -- admin only.",
        "parameters": {"type": "object", "properties": {}}}},
    _list_household, min_role="admin", data_scope="system", risk_tier="D"))

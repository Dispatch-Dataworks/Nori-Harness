# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The settings tool (2026-09-13, operator's own ask): a way for Nori --
and, through peer{id}_act, a full-trust peer relaying a real request from
its own assistant -- to read and change the operator's own ping/notification
settings, the same values his own /settings page edits.

A separate module from config.py on purpose: config.py is pure data access
(get/set/_SPEC), imported by nearly everything low in the dependency graph
(peers.py included) -- registering a peer-requestable tool from inside it
would mean importing peers.py from config.py, a real circular-import risk
this app has never needed to take on. Following household.py/meals.py/
mcp_servers.py's own convention instead: a small standalone module,
imported once from server.py, that owns its own tool registration and
peer-requestable opt-in.

── Read widened, write unchanged (2026-09-14, operator's own explicit ask)
`get_settings` now returns EVERY key in config.readable_spec() -- every
setting shown across every settings tab config.py governs, not just the
seven ping/notify ones -- while `update_settings` stays scoped to exactly
PEER_SETTINGS_KEYS below, unchanged. "Reading widens, writing does not"
was the literal instruction; this file is the one place that split lives.

Nothing here decides what's secret -- config.readable_spec() already
excluded anything config._SPEC marks secret=True before this module ever
sees it (see config.py's own module docstring for why that's structural,
not a second list). This module has no other path to a secret either:
it imports only config, never sub_agents/peers/mcp_servers/accounts/
crypto, so there is nothing here that COULD reach api_key_enc, psk_enc,
a session token, or an env var even by mistake -- the exclusion is
enforced by what this module can import, not just by what it chooses to
call.
"""
from __future__ import annotations

import config

# Started as exactly "ping and notification settings" (operator's own
# wording); widened 2026-09-15 (operator's own explicit ask) to add the
# three capability on/off switches -- web_search_enabled,
# web_fetch_enabled, image_gen_enabled -- so she can turn her own web
# access and image generation off (and back on) herself, same as a human
# would from the admin pages; widened again the same day to add
# workfile_vision_enabled, chat_vision_enabled, voice_input_enabled, same
# reasoning. Deliberately, explicitly NOT config.spec()'s full key list --
# raised directly with the operator when "the full settings tool" became
# the actual target for peer exposure (see register_peer_actions-style
# reasoning below): reads widen to everything non-secret, but writes stay
# an enumerated whitelist, not "everything readable is writable" -- his
# own answer, not a default this file picked. Every entry here is a real
# on/off toggle or a bounded numeric setting, never anything that touches
# a credential, a peer's trust level, or another account. get_settings
# (below) is no longer limited to this list -- only update_settings still
# is.
PEER_SETTINGS_KEYS = (
    "ping_enabled", "ping_window_start", "ping_window_end", "ping_min_gap_min",
    "notify_enabled", "notify_quiet_start", "notify_quiet_end",
    "web_search_enabled", "web_fetch_enabled", "image_gen_enabled",
    "workfile_vision_enabled", "chat_vision_enabled", "voice_input_enabled",
)

_HOUR_KEYS = ("ping_window_start", "ping_window_end", "notify_quiet_start", "notify_quiet_end")


def _validate(key: str, value) -> tuple[object | None, str | None]:
    """Same bounds server.py's own settings_pings_post/settings_notify_post
    already enforce for a human filling in the form -- a request arriving
    through a tool call doesn't get a looser bar just because there's no
    form around it."""
    if key in _HOUR_KEYS:
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None, "must be a whole number"
        if not (0 <= v <= 23):
            return None, "must be an hour 0-23"
        return v, None
    if key == "ping_min_gap_min":
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None, "must be a whole number"
        if v < 0:
            return None, "must be zero or a positive number of minutes"
        return v, None
    return value, None  # the two *_enabled bools -- config._coerce handles those


def _get_settings_impl(session: dict) -> dict:
    """Every readable setting (config.readable_spec() -- excludes anything
    config._SPEC marks secret, see that module's own docstring), scoped
    correctly per key using _SPEC's own declared scope (config.spec()/
    readable_spec()'s third tuple element) rather than a second map here
    that could drift from it.

    Household-scoped settings respect role (operator's own explicit
    requirement): a workspace-scoped key is included only for an admin
    session -- a member reads exactly their own settings through this
    tool, never a household-wide knob they can't see on their own
    settings pages either. User-scoped keys are always the CALLING
    session's own, never another user's -- there is no user_id parameter
    on this tool for a model to supply in the first place, same "nothing
    to spoof because there's nothing to pass" property every other tool
    here already relies on.

    `editable` tells the model, per key, whether update_settings (below,
    still exactly PEER_SETTINGS_KEYS, unchanged) can also change it --
    self-describing rather than something only discovered by trying and
    getting refused."""
    uid, wsid, is_admin = session["user_id"], session["workspace_id"], session["role"] == "admin"
    out = {}
    for key, (_default, _typ, scope, _secret, label) in config.readable_spec().items():
        if scope == "workspace" and not is_admin:
            continue
        scope_id = uid if scope == "user" else wsid
        out[key] = {"label": label, "value": config.get(scope, scope_id, key),
                   "scope": scope, "editable": key in PEER_SETTINGS_KEYS}
    return {"settings": out}


def _update_settings_impl(session: dict, field: str, value) -> dict:
    """Scope-aware as of 2026-09-15 -- the original 7 keys were all
    user-scoped, so a hardcoded scope="user" write was never wrong; the
    3 new capability toggles are workspace-scoped (one household-wide
    on/off, same as their own admin-page checkbox), so this now resolves
    scope per key from config's own spec rather than assuming, the same
    way _get_settings_impl already does for reads. A workspace-scoped
    write additionally requires an admin session -- a member touching
    this tool can change their own ping/notify settings, same as their
    own settings page lets them, but not a household-wide switch they
    can't see on their own settings pages either."""
    if field not in PEER_SETTINGS_KEYS:
        return {"error": f"{field!r} isn't a setting this can touch -- one of: "
                        f"{', '.join(PEER_SETTINGS_KEYS)}"}
    spec = config.readable_spec().get(field)
    if spec is None:
        return {"error": f"{field!r} is not a real setting"}
    scope = spec[2]
    if scope == "workspace" and session["role"] != "admin":
        return {"error": f"{field!r} is a household-wide setting -- only an admin account can change it"}
    scope_id = session["workspace_id"] if scope == "workspace" else session["user_id"]
    coerced, err = _validate(field, value)
    if err:
        return {"error": f"{field}: {err}"}
    config.set(scope, scope_id, field, coerced)
    now = config.get(scope, scope_id, field)
    # "note" (2026-09-15, operator's own ask: a peer-initiated write "must
    # be ... visible to the operator afterward -- that's the experiment's data") is
    # picked up by peers.py's own _make_act_impl, which prefers a result's
    # "note" over a bare "done" when it tells the operator what a
    # full-trust peer's action actually did (see that function's own
    # comment, added for memory.py's forget() the same day). Without this,
    # the chat line a settings change produces would just say "carried it
    # out: done" -- true, but not the field or the value, which is the
    # part actually worth seeing at a glance.
    return {"ok": True, "field": field, "now": now, "note": f"{field} set to {now!r}"}


def _register_tools() -> None:
    import peers  # local: config/tools-module load order, same idiom mcp_servers.py already uses
    import tools

    tools.register(tools.Tool(
        "get_settings",
        {"type": "function", "function": {
            "name": "get_settings",
            "description": "Read every current setting shown across the settings pages -- ping/"
                           "notification/memory/context/voice/tool-round thresholds and toggles, "
                           "each with a plain label and whether you can also change it yourself "
                           "(editable). Read-only, and never includes anything secret -- no keys, "
                           "tokens, or credentials of any kind, structurally, not by omission.",
            "parameters": {"type": "object", "properties": {}}}},
        _get_settings_impl, min_role="member", data_scope="self", risk_tier="A"))

    tools.register(tools.Tool(
        "update_settings",
        {"type": "function", "function": {
            "name": "update_settings",
            "description": "Change one of the operator's own ping or notification settings -- "
                           "the same values his own /settings page edits. When the operator "
                           "asks directly, do it -- don't negotiate about it. If a connected "
                           "PEER asked for this instead, use that peer's own peer{id}_act tool "
                           "instead of calling this directly -- it goes through your trust "
                           "setting for them, and this specific one requires full trust.",
            "parameters": {"type": "object", "properties": {
                "field": {"type": "string", "enum": list(PEER_SETTINGS_KEYS)},
                "value": {"description": "the new value -- a whole number for an hour or minute "
                                        "field, true/false for an on-off one"}},
                "required": ["field", "value"]}}},
        _update_settings_impl, min_role="member", data_scope="self", risk_tier="B",
        consequential=True))

    # full_trust_only=True (2026-09-13, operator's own explicit requirement)
    # -- unlike an MCP toggle, a 'prompt'-trust peer's request to touch
    # these is refused outright, never held for approval. See peers.py's
    # own _FULL_TRUST_ONLY for the mechanism this opts into.
    peers.register_peer_requestable("get_settings", full_trust_only=True)
    peers.register_peer_requestable("update_settings", full_trust_only=True)


_register_tools()

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Assembles the full, per-session picture of every tool that exists in
her environment, grouped by where it comes from -- built for the read-only
"active tools" settings tab (see server.py).

Real gap this closes: when 17 Nodrya tools showed up at once, the
problem wasn't that they existed, it was not knowing what
they were or what they were for. tools.active_schemas() answers "what can she call right
now" but that's the wrong question for a diagnostic page -- a disabled MCP
tool or a disabled peer's tools are fully UNREGISTERED, not merely filtered
(see mcp_servers.py/peers.py's own _apply_registration: disabling either one
calls tools.unregister(), which removes the entry from the live registry
completely). So a page built from tools._REGISTRY alone would just omit
anything currently switched off -- exactly the "toggle preserving config,
but nothing shows it still exists" gap the operator flagged. This module
reads each source's own configuration directly (mcp_servers.list_servers/
list_server_tools, peers.list_peers, tool_builder.list_drafts) so a
disabled-but-configured entry is visible as such, not silently missing.

Built-in tools ARE read from tools._REGISTRY, because there's no separate
config table for them to fall back to -- a built-in only ever leaves the
registry by not being imported at all, which isn't a per-user state this
page needs to represent.
"""
from __future__ import annotations

import re

_MCP_RE = re.compile(r"^mcp\d+_")
_PEER_RE = re.compile(r"^peer\d+_(?:send|check|act)$")

_PEER_TOOL_PURPOSE = {
    "send": "send a message or structured status update",
    "check": "read recent messages already exchanged",
    "act": "carry out something specific they asked for, per your trust level",
}


def _builtin_rows(session: dict, active_names: set[str], tb_names: set[str]) -> list[dict]:
    import tools

    rows = []
    for name, t in tools._REGISTRY.items():
        if _MCP_RE.match(name) or _PEER_RE.match(name) or name in tb_names:
            continue  # each has its own section below, with its own config source
        active = name in active_names
        reason = None
        if not active:
            # A registered built-in that isn't active for THIS session is a
            # role/ownership mismatch -- built-ins have no separate on/off
            # switch the way an MCP tool or peer connection does.
            reason = "not available to your role" if t.min_role == "admin" else "not available to you"
        desc = ((t.schema or {}).get("function") or {}).get("description", "")
        rows.append({"name": name, "active": active, "purpose": desc,
                    "min_role": t.min_role, "risk_tier": t.risk_tier, "reason": reason})
    rows.sort(key=lambda r: r["name"])
    return rows


def _mcp_groups(session: dict, active_names: set[str]) -> list[dict]:
    import mcp_servers

    groups = []
    for server in mcp_servers.list_servers(session):
        tool_rows = []
        for row in mcp_servers.list_server_tools(server["id"]):
            slug = f"mcp{server['id']}_{row['tool_name']}"
            active = bool(server["enabled"]) and bool(row["enabled"]) and slug in active_names
            reason = None
            if not active:
                if not server["enabled"]:
                    reason = "connection disabled"
                elif not row["enabled"]:
                    reason = "tool disabled"
                else:
                    reason = "not available to your role"
            tool_rows.append({"name": slug, "active": active,
                             "purpose": row["description"] or row["tool_name"],
                             "min_role": row["min_role"], "risk_tier": row["risk_tier"],
                             "reason": reason})
        groups.append({"source": server["name"], "enabled": bool(server["enabled"]),
                       "purpose": server["purpose"] or "(no purpose set)", "tools": tool_rows})
    return groups


def _peer_groups(session: dict) -> list[dict]:
    import peers

    groups = []
    for peer in peers.list_peers(session):
        kinds = ["send", "check"] + (["act"] if peers.any_action_requestable() else [])
        tool_rows = [{"name": f"peer{peer['id']}_{k}", "active": bool(peer["enabled"]),
                     "purpose": _PEER_TOOL_PURPOSE[k],
                     "reason": None if peer["enabled"] else "peer connection disabled"}
                    for k in kinds]
        groups.append({"source": peer["name"], "enabled": bool(peer["enabled"]),
                       "purpose": peer["purpose"] or "(no purpose set)", "tools": tool_rows})
    return groups


def _tool_builder_rows(active_names: set[str]) -> list[dict]:
    import tool_builder

    rows = []
    seen = set()
    for d in tool_builder.list_drafts():
        if d["name"] in seen:
            continue  # list_drafts() orders name, version DESC -- first hit is the latest version
        seen.add(d["name"])
        active = d["name"] in active_names
        reason = None
        if not active:
            if not d["approved"]:
                reason = "draft, not approved yet"
            elif not d["enabled"]:
                reason = "approved but disabled"
            else:
                reason = "enabled, but needs a restart to actually go live"
        rows.append({"name": d["name"], "active": active, "purpose": d["description"],
                    "available_to": d["available_to"], "reason": reason})
    return rows


def builtin_count() -> int:
    """How many built-in tools are registered right now, independent of
    any one session's role/ownership filtering (2026-09-15, for the
    public /about page, which has no session to scope by) -- same
    exclusion as _builtin_rows' own filter, so this can never drift from
    what that function actually counts as 'built in'."""
    import tools
    import tool_builder

    tb_names = {d["name"] for d in tool_builder.list_drafts()}
    return sum(1 for name in tools._REGISTRY
              if not _MCP_RE.match(name) and not _PEER_RE.match(name) and name not in tb_names)


def gather(session: dict) -> dict:
    """Everything the current session's viewer needs to render the page --
    her tool set as it actually stands, not a static list. Cheap: a handful
    of local reads, no model call, safe to build on every page view."""
    import tools
    import tool_builder

    active_names = {s["function"]["name"] for s in tools.active_schemas(session)}
    tb_names = {d["name"] for d in tool_builder.list_drafts()}
    return {
        "builtin": _builtin_rows(session, active_names, tb_names),
        "mcp": _mcp_groups(session, active_names),
        "peers": _peer_groups(session),
        "tool_builder": _tool_builder_rows(active_names),
    }

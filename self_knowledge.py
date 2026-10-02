# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""A tool for Nori to look up how she herself works (2026-09-15,
operator's own ask) -- the recurring problem this responds to: she has
capabilities she doesn't reliably know the purpose or limits of (17
Nodrya tools landing at once with no idea what they were for --
a tool registered isn't a tool understood), and
the one time she reasoned correctly about her own tooling (the security
review blocker) it was because she happened to already know the shape
of the thing. This turns that from luck into something she can do on
purpose.

Deliberately NOT hand-written prose describing each subsystem: every
topic below reads from the real, already-existing source for that
subsystem (capabilities.gather() for tools, memory.TYPES/TYPE_HELP for
memory, config.readable_spec() for the tunable numbers, peers.py's own
TRUST_LEVEL_MEANING and compaction.py's own COMPACTION_EXPLAIN and
jobs.py's own SUBAGENT_LIMITS_EXPLAIN for the two genuine gaps that had
no importable source before this). If any of those change, this tool's
answer changes with them automatically -- nothing here is a second copy
that can quietly drift from what's actually true.

Same secret-exclusion discipline as settings_tool.py, and for the same
structural reason: this module imports only capabilities/memory/config/
compaction/jobs/sub_agents/peers -- never crypto, never accounts -- and
where a source function's own row includes something sensitive
(sub_agents.list_all()'s api_key_enc, peers.list_peers()'s psk_enc),
this module projects only the safe fields itself, the same way
capabilities.py's own _peer_groups() already does, rather than passing
a raw row through."""
from __future__ import annotations

import os

_TOPICS = ("tools", "memory", "compaction", "paci", "sub_agents", "identity")

# Every real page a person (or she) can actually navigate to on this
# instance, right now (2026-09-19, operator's own ask -- the direct fix
# for a real confabulation instance: she asserted a settings-page
# "about" existed before one did, and separately didn't know /about
# itself was real). Hand-maintained, like _hdr_menu's own identical
# list in server.py (a page isn't the kind of thing config.py/tools.py
# already enumerate somewhere importable) -- kept short and accurate
# rather than derived, on purpose; update this alongside server.py's
# own do_GET whenever a real top-level page is added or removed.
_PAGES = {
    "/": "Chat -- the daily conversation.",
    "/inventory": "Household inventory.",
    "/meals": "Meal planning.",
    "/trackers": "Duration trackers.",
    "/files": "The shared working folder.",
    "/history": "Full, searchable conversation history.",
    "/photos": "Every image ever sent, as a grid.",
    "/settings": "Personal preferences, plus (for an admin) household/instance "
                 "configuration -- grouped into sections, including an 'About' "
                 "one with this instance's own version/uptime/model/storage.",
    "/about": "Public, unauthenticated -- what she is, for someone evaluating "
             "or self-hosting her, not the household using her day to day.",
}


def _tools_summary(session: dict) -> dict:
    import capabilities

    data = capabilities.gather(session)
    builtin = [{"name": r["name"], "purpose": r["purpose"], "active": r["active"]}
              for r in data["builtin"]]
    mcp = [{"source": g["source"], "enabled": g["enabled"], "purpose": g["purpose"],
           "tools": [{"name": t["name"], "purpose": t["purpose"], "active": t["active"]}
                    for t in g["tools"]]}
          for g in data["mcp"]]
    peer_tools = [{"source": g["source"], "enabled": g["enabled"], "purpose": g["purpose"],
                  "tools": [{"name": t["name"], "purpose": t["purpose"]} for t in g["tools"]]}
                 for g in data["peers"]]
    return {"builtin": builtin, "mcp_connections": mcp, "peer_connections": peer_tools,
           "note": "active=false means registered but not currently callable -- see each "
                   "entry's own reason (role/ownership, disabled, unapproved, or needs a "
                   "restart); it was never silently missing, just not on right now."}


def _memory_summary() -> dict:
    import memory

    return {"types": {t: memory.TYPE_HELP[t] for t in memory.TYPES},
           "always_loaded": list(memory.ALWAYS_LOAD_TYPES),
           "note": "always_loaded types ride along on every turn automatically; everything "
                   "else is only in context when recall() actually pulls it -- reach for "
                   "recall(type=...) rather than assuming a fact you stored is already there."}


def _compaction_summary(session: dict) -> dict:
    import compaction
    import config

    wsid = session["workspace_id"]
    numbers = {k: config.get("workspace", wsid, k) for k in (
        "context_window_msgs", "compaction_enabled", "compaction_max_segments",
        "compaction_budget_tokens", "compaction_session_gap_hours")}
    return {"how_it_works": compaction.COMPACTION_EXPLAIN, "current_settings": numbers}


def _paci_summary(session: dict) -> dict:
    import peers

    own_peers = [{"name": p["name"], "enabled": bool(p["enabled"]),
                 "purpose": p["purpose"] or "(no purpose set)", "trust_level": p["trust_level"],
                 "what_that_trust_level_means": peers.TRUST_LEVEL_MEANING[p["trust_level"]]}
                for p in peers.list_peers(session)]
    return {"what_it_is": (
                "PACI is the protocol connecting you to another agent (a peer) the operator "
                "has deliberately paired you with -- a symmetric, ongoing conversation, not a "
                "tool you call into. Ordinary messages from a peer are screened for prompt "
                "injection on arrival (unless the operator has explicitly turned screening off "
                "for that specific peer) and folded into your own context automatically; you "
                "don't need to poll for them. reply_requested is the one exception to the "
                "normal rule that receiving something never itself triggers a turn -- it "
                "compels you to take a turn right now, never an answer. Ending that turn "
                "without replying is a legitimate outcome, not a fault."),
            "your_connections": own_peers,
            "trust_levels_in_general": dict(peers.TRUST_LEVEL_MEANING)}


def _sub_agents_summary() -> dict:
    import jobs
    import models
    import sub_agents

    model_alias_by_id = {m["id"]: m["alias"] for m in models.list_all()}
    roster = [{"label": a["label"], "model": model_alias_by_id.get(a["model_id"], "(none configured)"),
              "enabled": bool(a["enabled"]),
              "tool_call_limit": a["tool_call_limit"], "tool_byte_limit": a["tool_byte_limit"],
              "file_write": bool(a["file_write"]), "web_access": bool(a["web_access"])}
             for a in sub_agents.list_all()]
    return {"what_they_can_do": jobs.SUBAGENT_LIMITS_EXPLAIN, "configured_roster": roster}


def _identity_summary() -> dict:
    """Her own real address and real page list (2026-09-19, operator's
    own ask, tied directly to two real gaps found the same day: she
    couldn't answer "what's your address" at all, having no way to read
    NORI_PUBLIC_URL, and separately invented a settings-page "about"
    that didn't exist yet -- while apparently not knowing /about
    itself, which already did. public_url is read the same way
    server.py's own PUBLIC_URL is (there's no importable shared
    constant without a circular import back into server.py, which
    imports this module) -- if server.py's own computation ever grows
    past a plain env read, this needs updating alongside it."""
    public_url = os.environ.get("NORI_PUBLIC_URL", "").rstrip("/")
    return {
        "public_url": public_url or None,
        "public_url_note": ("your real, reachable address, if the operator has set one -- give "
                            "this plainly when asked your URL/address rather than guessing at "
                            "one. None means NORI_PUBLIC_URL isn't configured yet, which is a "
                            "real, sayable answer too, not a failure to paper over."),
        "pages": _PAGES,
        "pages_note": ("every real page that exists on this instance, right now. Don't assert a "
                      "page exists -- or invent one that sounds plausible, like a second "
                      "'about' page living somewhere it doesn't -- without checking this list "
                      "first."),
    }


def _overview() -> dict:
    return {
        "tools": "every tool you have -- built in, from a connected MCP server, or from a "
                "connected peer -- with its real purpose and whether it's actually callable "
                "right now. Ask for topic='tools' before assuming what a tool does or "
                "guessing you don't have one.",
        "memory": "the fixed taxonomy your memories are typed by, and which types load "
                 "automatically versus only on recall(). Ask for topic='memory'.",
        "compaction": "how older conversation gets summarized instead of simply dropped, and "
                      "the current tunable numbers. Ask for topic='compaction'.",
        "paci": "what the peer-agent protocol is, your own connections and their trust levels, "
               "and what each trust level actually permits. Ask for topic='paci'.",
        "sub_agents": "what a background sub-agent job can and can't do, and the currently "
                      "configured roster. Ask for topic='sub_agents'.",
        "identity": "your own real address (if the operator has set one) and every real page "
                   "that exists on this instance. Ask for topic='identity'.",
        "note": "call this again with one of these as `topic` for the real detail -- this "
                "overview is deliberately short.",
    }


def explain_self_impl(session: dict, topic: str | None = None) -> dict:
    if not topic:
        return _overview()
    if topic not in _TOPICS:
        return {"error": f"topic must be one of: {', '.join(_TOPICS)} (or omit it for an overview)"}
    if topic == "tools":
        return _tools_summary(session)
    if topic == "memory":
        return _memory_summary()
    if topic == "compaction":
        return _compaction_summary(session)
    if topic == "paci":
        return _paci_summary(session)
    if topic == "identity":
        return _identity_summary()
    return _sub_agents_summary()


def _register_tools() -> None:
    import tools

    tools.register(tools.Tool(
        "explain_self",
        {"type": "function", "function": {
            "name": "explain_self",
            "description": (
                "Look up how you actually work, read live from the real source for each thing "
                "rather than guessed at -- your real tool list and what each is for, how memory "
                "types work, what compaction does to old conversation, what PACI/trust levels "
                "mean including your own connections, what a sub-agent job can and can't do, "
                "and (topic='identity') your own real address and every real page that exists "
                "on this instance. Reach for this BEFORE explaining your own capabilities to "
                "anyone (the operator, a peer, yourself), before assuming you can't do "
                "something, before asserting a page or address exists, or when asked directly "
                "what you're capable of or how to reach you -- guessing gets this wrong in "
                "exactly the way this tool exists to prevent. Omit `topic` for a short index of "
                "what's available; pass one to get the real detail."),
            "parameters": {"type": "object", "properties": {
                "topic": {"type": "string", "enum": list(_TOPICS),
                         "description": "one of the listed topics, or omit for an overview"}}}}},
        explain_self_impl, min_role="member", data_scope="self", risk_tier="A"))


_register_tools()

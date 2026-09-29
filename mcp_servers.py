# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""MCP client support -- the only module with raw SQL against
`mcp_servers`/`mcp_server_tools`. Owns the whole path from "an admin adds
a server connection" to "a tool from that server is a real, dispatched
Tool" -- the design reasoning behind a hand-rolled client, tools landing
locked-down by default, and results going through ingest.py rather than
reaching the model raw.

Nothing here trusts the remote server with anything it hasn't earned:
- A newly-discovered tool is registered enabled but hard-locked to
  risk_tier='D' (admin-only, the same floor tools.py itself enforces for
  every other Tier D tool) until a human reviews it and deliberately
  widens min_role/data_scope/risk_tier. The server's own tool description
  is context for that human, never a permission grant.
- Every result an MCP tool returns is untrusted content, full stop --
  routed through ingest.summarize_untrusted(..., preserve_content=True)
  before it ever reaches the model, same defense email/working-folder
  content already gets. A compromised or simply hostile MCP server is a
  prompt-injection vector like any other untrusted source; this is not
  optional per-tool, it's unconditional for every tool from every server.
- Some servers put their whole auth credential in the connection URL's
  path (auth_type='url_embedded', e.g. Nodrya) rather than a header --
  for those, `url` and `credential` are the same secret, not two. The
  plain `url` column NEVER holds that real value, only mask_url()'s
  display form; the real one lives solely in credential_enc, encrypted,
  and only _connect_url() ever reconstructs it for an actual call.
"""
from __future__ import annotations

import json
import time

import accounts
import crypto
import ingest
import mcp_client
import store
import tools

_VALID_SCOPES = ("user", "workspace")
# 'url_embedded' -- some servers (Nodrya included) issue a unique-per-
# account URL with the credential living in the path itself, no separate
# header at all. The schema below (url + credential_enc as two separate
# columns) assumed auth lived in a header; for this type they're the same
# secret, so the REAL url is never put in the plain `url` column at all --
# see mask_url()/_connect_url() below.
_VALID_AUTH_TYPES = ("none", "bearer", "url_embedded")


def mask_url(url: str) -> str:
    """The display form for a url_embedded connection -- and, structurally,
    the ONLY form of the url this app ever writes to the plain `url`
    column for that auth type (the real one lives solely in
    credential_enc, encrypted). Keeps scheme/host/path shape but reduces
    the final path segment (where a per-account token like Nodrya's lives)
    to a short, non-reversible prefix -- enough to tell two connections
    apart in a list, not enough to reconstruct the real value from."""
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(url)
    segments = parts.path.strip("/").split("/")
    if segments and segments[-1]:
        last = segments[-1]
        segments[-1] = (last[:6] + "…redacted") if len(last) > 6 else "…redacted"
    masked_path = "/" + "/".join(segments)
    return urlunsplit((parts.scheme, parts.netloc, masked_path, "", ""))


# ── storage ──────────────────────────────────────────────────────────────
def create_server(session: dict, *, scope: str, name: str, url: str,
                  auth_type: str = "none", credential: str | None = None,
                  purpose: str | None = None) -> dict:
    """scope='user' scopes the connection (and its credential) to the
    creating admin's own account -- scope_id is their user_id, and only
    their own session ever supplies this credential on a call. scope=
    'workspace' shares one connection (and one credential) across the
    whole household -- see the module docstring on what that does and
    doesn't guarantee about per-user data separation on the REMOTE
    server's side, which this app has no way to enforce if the server
    itself has no concept of individual callers.

    For auth_type='url_embedded', `url` IS the credential -- there is no
    separate `credential` argument for it, and the real value never
    touches the plain `url` column (see mask_url()).

    `purpose` -- what the server actually IS, in the admin's own words
    ("the operator's personal notes app"). Not required, but strongly worth
    supplying: a bare connection name and a function name were found to
    tell Nori nothing about what a connection is FOR (see
    capability_block())."""
    name = (name or "").strip()
    url = (url or "").strip()
    purpose = (purpose or "").strip() or None
    if not name or not url:
        return {"error": "name and url are required"}
    if scope not in _VALID_SCOPES:
        return {"error": f"scope must be one of: {', '.join(_VALID_SCOPES)}"}
    if auth_type not in _VALID_AUTH_TYPES:
        return {"error": f"auth_type must be one of: {', '.join(_VALID_AUTH_TYPES)}"}
    if auth_type == "bearer" and not credential:
        return {"error": "auth_type=bearer needs a credential"}
    scope_id = session["user_id"] if scope == "user" else session["workspace_id"]
    if auth_type == "url_embedded":
        stored_url, cred_enc = mask_url(url), crypto.encrypt(url)
    elif auth_type == "bearer":
        stored_url, cred_enc = url, crypto.encrypt(credential)
    else:
        stored_url, cred_enc = url, None
    now = time.time()

    def _w(c):
        return c.execute(
            "INSERT INTO mcp_servers(scope, scope_id, name, url, auth_type, credential_enc, "
            "purpose, enabled, created_ts, created_by) VALUES (?,?,?,?,?,?,?,1,?,?)",
            (scope, scope_id, name, stored_url, auth_type, cred_enc, purpose, now,
             session["user_id"])).lastrowid
    server_id = store.write(_w)
    return {"ok": True, "server_id": server_id}


def list_all_servers() -> list[dict]:
    """Every server, any scope/owner -- for integration_health.py's own
    instance-wide sweep, which (unlike list_servers(session) below) isn't
    scoped to one session's ownership: a health check is operational
    visibility into whether a connection is working, not a data-access
    grant, so it has no reason to hide a workspace- or another user's
    scoped connection from the one admin-facing health page. Never
    returns a credential -- same rows list_servers already returns."""
    rows = store.read(lambda c: c.execute("SELECT * FROM mcp_servers ORDER BY id").fetchall())
    return [dict(r) for r in rows]


def check_health(server_id: int) -> dict:
    """The liveness+auth probe for one connection -- a real tools/list
    round trip, the exact same call sync_tools() already makes, reused
    here rather than inventing a second way to talk to an MCP server
    (integration_health.py's own "follow the PACI pattern, don't invent
    a second shape" instruction, applied inward to this module too).
    Deliberately does NOT reconcile mcp_server_tools the way sync_tools()
    does -- a health check must never silently change what's registered
    as a side effect of just checking whether the server is alive.

    mcp_client.MCPError's own message text is deliberately generic (see
    that module's docstring: some servers, Nodrya included, put the real
    credential IN the url, so no error path there ever echoes url/
    request/response detail) -- but it DOES always name a real HTTP
    status when the server actually answered ("server returned HTTP
    401"), which is enough to classify without needing a second return
    shape from that module. Returns {"status", "detail", "explain"} in
    integration_health.py's own shared vocabulary -- explain (2026-09-19)
    is the short, relayable "what it means and what would fix it" line,
    written in Nori's own first-person agent register (see that module's
    docstring on why: she names her own machinery plainly)."""
    server = get_server(server_id)
    if server is None:
        return {"status": "not_configured", "detail": "connection no longer exists",
                "explain": "That MCP connection no longer exists."}
    name = server["name"]
    if not server["enabled"]:
        return {"status": "not_configured", "detail": "connection is disabled",
                "explain": f"My connection to {name} is turned off -- an admin can re-enable it from Settings > MCP servers."}
    try:
        mcp_client.list_tools(_connect_url(server), headers=_auth_headers(server))
    except mcp_client.MCPError as exc:
        text = str(exc)
        if "HTTP 401" in text:
            return {"status": "auth_expired", "detail": text,
                    "explain": f"My connection to {name} looks wrong or has expired -- an admin needs to check its credential in Settings > MCP servers."}
        if "HTTP 403" in text:
            return {"status": "scope_missing", "detail": text,
                    "explain": f"{name} is reachable but refused my request -- it may need a permission change on its own side."}
        if "HTTP 429" in text:
            return {"status": "rate_limited", "detail": text,
                    "explain": f"{name} is rate-limiting my requests right now -- nothing's broken, worth waiting and retrying."}
        if text.startswith(("could not reach", "server did not respond")):
            return {"status": "unreachable", "detail": text,
                    "explain": f"I can't reach {name} right now -- it may be down or unreachable."}
        return {"status": "error", "detail": text,
                "explain": f"{name} returned something I don't have a specific read on: {text}"[:300]}
    return {"status": "healthy", "detail": "tools/list responded",
            "explain": f"My connection to {name} is working normally."}


def list_servers(session: dict) -> list[dict]:
    """Every server this session can see -- their own user-scoped
    connections plus every workspace-scoped one for their household.
    Credentials never leave this module: this list is for display/admin
    management, not for building an auth header (see _auth_headers)."""
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM mcp_servers WHERE (scope='user' AND scope_id=?) "
        "OR (scope='workspace' AND scope_id=?) ORDER BY id",
        (session["user_id"], session["workspace_id"])).fetchall())
    return [dict(r) for r in rows]


def get_server(server_id: int) -> dict | None:
    r = store.read(lambda c: c.execute("SELECT * FROM mcp_servers WHERE id=?", (server_id,)).fetchone())
    return dict(r) if r else None


def _owns_server(session: dict, server: dict) -> bool:
    if server["scope"] == "user":
        return server["scope_id"] == session["user_id"]
    return server["scope_id"] == session["workspace_id"]


def set_server_enabled(session: dict, server_id: int, enabled: bool) -> dict:
    server = get_server(server_id)
    if server is None or not _owns_server(session, server):
        return {"error": "no such server"}
    store.write(lambda c: c.execute("UPDATE mcp_servers SET enabled=? WHERE id=?",
                                    (1 if enabled else 0, server_id)))
    _apply_registration(server_id)
    return {"ok": True}


def set_purpose(session: dict, server_id: int, purpose: str) -> dict:
    """Editable after the fact -- a connection made before this field
    existed (Nodrya, the first one) needs a way to acquire one without
    being deleted and recreated. Re-registers nothing itself: purpose
    only ever reaches capability_block()'s system-prompt text, not any
    Tool's own schema, so there's no live registration to refresh here."""
    server = get_server(server_id)
    if server is None or not _owns_server(session, server):
        return {"error": "no such server"}
    store.write(lambda c: c.execute("UPDATE mcp_servers SET purpose=? WHERE id=?",
                                    ((purpose or "").strip() or None, server_id)))
    return {"ok": True}


def capability_block(user_id: int) -> str:
    """A short system-prompt block naming every connected, enabled server
    this user can actually use (same ownership rule _owner_check_for
    enforces at dispatch time -- if she can't call it, don't tell her
    about it either) and what it's FOR, in the admin's own words.

    Exists because a tool being registered isn't the same as her knowing
    what it gives her: `[nodrya] search the user's notes` is a function
    name, not an explanation that Nodrya is a personal notes app worth
    reaching into unprompted -- confirmed as a real gap, not a
    hypothetical one: she had these tools live in her own schema and
    still described wanting the capability they already gave her.
    Generated fresh from the connection every time this is
    called, so it can never drift from what's actually connected the way
    hand-maintained prose would."""
    user = accounts.get_user(user_id)
    if user is None:
        return ""
    session = {"user_id": user_id, "workspace_id": user["workspace_id"], "role": user["role"]}
    rows = store.read(lambda c: c.execute("SELECT * FROM mcp_servers WHERE enabled=1").fetchall())
    lines = []
    for server in (dict(r) for r in rows):
        if not _owner_check_for(server)(session):
            continue
        prefix = f"mcp{server['id']}_"
        purpose = server["purpose"] or f"the {server['name']} connector (no description set yet)"
        lines.append(f"- {server['name']} (her tools for it are named {prefix}*): {purpose}")
    if not lines:
        return ""
    return "Connected external tools:\n" + "\n".join(lines)


def delete_server(session: dict, server_id: int) -> dict:
    server = get_server(server_id)
    if server is None or not _owns_server(session, server):
        return {"error": "no such server"}
    tool_rows = store.read(lambda c: c.execute(
        "SELECT tool_name FROM mcp_server_tools WHERE server_id=?", (server_id,)).fetchall())
    for t in tool_rows:
        tools.unregister(_tool_slug(server_id, t["tool_name"]))
    store.write(lambda c: c.execute("DELETE FROM mcp_server_tools WHERE server_id=?", (server_id,)))
    store.write(lambda c: c.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,)))
    return {"ok": True}


def list_server_tools(server_id: int) -> list[dict]:
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM mcp_server_tools WHERE server_id=? ORDER BY tool_name", (server_id,)).fetchall())
    return [dict(r) for r in rows]


def set_tool_grant(session: dict, server_id: int, tool_name: str, *,
                   min_role: str, data_scope: str, risk_tier: str, enabled: bool) -> dict:
    """The deliberate-widening action -- a human reviewing exactly one
    discovered tool and deciding it's safe to loosen. Validated the same
    way tools.Tool() itself validates (invalid combination raises there,
    not silently accepted here), so a bad row can never be written that
    would then fail confusingly at registration time instead of here."""
    server = get_server(server_id)
    if server is None or not _owns_server(session, server):
        return {"error": "no such server"}
    try:
        tools.Tool(f"validate:{tool_name}", {}, lambda *a, **k: None,
                  min_role=min_role, data_scope=data_scope, risk_tier=risk_tier)
    except ValueError as exc:
        return {"error": str(exc)}
    # A no-op UPDATE (tool_name doesn't actually match a row -- a caller
    # bug, e.g. a stray trailing character, is exactly how this was first
    # found) must not report success: check existence first rather than
    # trusting rowcount from the same write() call, which store.write()
    # doesn't surface anyway.
    exists = store.read(lambda c: c.execute(
        "SELECT 1 FROM mcp_server_tools WHERE server_id=? AND tool_name=?",
        (server_id, tool_name)).fetchone())
    if exists is None:
        return {"error": f"no such tool on this connection: {tool_name!r}"}
    store.write(lambda c: c.execute(
        "UPDATE mcp_server_tools SET min_role=?, data_scope=?, risk_tier=?, enabled=? "
        "WHERE server_id=? AND tool_name=?",
        (min_role, data_scope, risk_tier, 1 if enabled else 0, server_id, tool_name)))
    _apply_registration(server_id)
    return {"ok": True}


# ── discovery ────────────────────────────────────────────────────────────
def _connect_url(server: dict) -> str:
    """The real, usable URL for an actual network call. For every auth
    type EXCEPT url_embedded, that's just server['url'] -- for
    url_embedded, server['url'] holds only the masked display form
    (mask_url()'s output, written at create_server() time and never
    replaced with the real value), so the real one has to come from
    decrypting credential_enc instead. Every real call site (sync_tools,
    the dispatch wrapper) must go through this, never read server['url']
    directly, or a url_embedded connection would try to call its own
    redacted placeholder."""
    if server["auth_type"] == "url_embedded" and server["credential_enc"]:
        return crypto.decrypt(server["credential_enc"])
    return server["url"]


def _auth_headers(server: dict) -> dict:
    if server["auth_type"] == "bearer" and server["credential_enc"]:
        return {"Authorization": f"Bearer {crypto.decrypt(server['credential_enc'])}"}
    return {}


def sync_tools(session: dict, server_id: int) -> dict:
    """Calls the real server's tools/list and reconciles mcp_server_tools
    against it. A tool discovered here for the first time lands ENABLED
    but locked to risk_tier='D' (admin-only) -- live immediately, but
    unusable by anyone but an admin until deliberately widened via
    set_tool_grant. A tool that disappears from the server's own list is
    left in place, disabled, rather than deleted outright -- the audit
    trail of "this server used to expose X" is worth keeping even if X
    is gone; deletion is still available via delete_server for the whole
    connection."""
    server = get_server(server_id)
    if server is None or not _owns_server(session, server):
        return {"error": "no such server"}
    try:
        discovered = mcp_client.list_tools(_connect_url(server), headers=_auth_headers(server))
    except mcp_client.MCPError as exc:
        return {"error": f"could not reach server: {exc}"}
    now = time.time()
    existing = {t["tool_name"] for t in list_server_tools(server_id)}
    seen = set()
    for t in discovered:
        name = t.get("name")
        if not name:
            continue
        seen.add(name)
        schema_json = json.dumps(t.get("inputSchema") or {"type": "object", "properties": {}})
        desc = (t.get("description") or "")[:500]
        if name in existing:
            store.write(lambda c, name=name, desc=desc, schema_json=schema_json: c.execute(
                "UPDATE mcp_server_tools SET description=?, input_schema=? "
                "WHERE server_id=? AND tool_name=?", (desc, schema_json, server_id, name)))
        else:
            store.write(lambda c, name=name, desc=desc, schema_json=schema_json: c.execute(
                "INSERT INTO mcp_server_tools(server_id, tool_name, description, input_schema, "
                "min_role, data_scope, risk_tier, enabled, discovered_ts) "
                "VALUES (?,?,?,?,'admin','self','D',1,?)",
                (server_id, name, desc, schema_json, now)))
    gone = existing - seen
    for name in gone:
        store.write(lambda c, name=name: c.execute(
            "UPDATE mcp_server_tools SET enabled=0 WHERE server_id=? AND tool_name=?",
            (server_id, name)))
    _apply_registration(server_id)
    return {"ok": True, "discovered": len(seen), "removed": len(gone)}


# ── registration into tools.py ───────────────────────────────────────────
def _tool_slug(server_id: int, tool_name: str) -> str:
    """Deterministic, collision-free across servers -- the model-facing
    name is a little technical (mcp3_read_note) rather than pretty, but
    uniqueness across however many servers get connected matters more
    than prettiness, and a tool's own description carries the readable
    context anyway."""
    return f"mcp{server_id}_{tool_name}"


def _make_impl(server_id: int, tool_name: str, server_name: str):
    def _impl(session: dict, **kwargs) -> dict:
        server = get_server(server_id)
        if server is None or not server["enabled"]:
            return {"error": f"the {server_name} connection is currently disabled"}
        # Second line of defense, same reasoning tools.dispatch() already
        # applies to user_id/workspace_id in arguments: dispatch() checks
        # owner_check() before ever reaching here, but this holds even if
        # _impl were ever called some other way, or a future server type
        # got registered without one.
        if not _owner_check_for(server)(session):
            return {"error": "not permitted for this account"}
        try:
            result = mcp_client.call_tool(_connect_url(server), tool_name, kwargs,
                                          headers=_auth_headers(server))
        except mcp_client.MCPError as exc:
            return {"error": f"{server_name} is unavailable right now: {exc}"}
        text = "\n".join(block.get("text", "") for block in (result.get("content") or [])
                        if block.get("type") == "text") or "(no text content returned)"
        # Every byte of that text is untrusted, regardless of what the
        # server claims about itself -- same pipeline email/working-folder
        # content already goes through, not a lighter-touch variant just
        # because this call happened to succeed.
        screened = ingest.summarize_untrusted(
            text, kind=f"tool result from the {server_name} MCP server", preserve_content=True)
        if result.get("isError"):
            screened["tool_reported_error"] = True
        return screened
    return _impl


def _owner_check_for(server: dict):
    """A tool bound to one specific connection has to stay bound to
    whoever that connection belongs to -- min_role/data_scope alone don't
    do this (see tools.py's module docstring: they were never independent
    runtime gates, every native tool is self-scoping by construction, and
    an MCP tool bound to a fixed server_id at registration time is the one
    real exception). Discovered as a genuine gap, not assumed closed: a
    scope='user' connection's tools were reachable by ANY member-level
    session before this existed, using the connection OWNER's credential
    regardless of who actually called it -- exactly the privacy hole
    "member-callable" must never mean here. scope='workspace' gets the
    equivalent check against workspace_id, for the same reason, even
    though a single-workspace deployment makes it moot today."""
    scope, scope_id = server["scope"], server["scope_id"]
    if scope == "user":
        return lambda session: session["user_id"] == scope_id
    return lambda session: session["workspace_id"] == scope_id


def _apply_registration(server_id: int) -> None:
    """Re-registers every tool for one server against its CURRENT rows --
    called after sync/enable/disable/widen so a change takes effect
    immediately, without needing the "restart to go live" the tool
    builder needs (there's no static-analysis gate here to re-run; the
    grant itself, just written, IS the safety check)."""
    server = get_server(server_id)
    for row in list_server_tools(server_id):
        slug = _tool_slug(server_id, row["tool_name"])
        if server is None or not server["enabled"] or not row["enabled"]:
            tools.unregister(slug)
            continue
        schema = {"type": "function", "function": {
            "name": slug,
            "description": f"[{server['name']}] {row['description'] or row['tool_name']}",
            "parameters": json.loads(row["input_schema"]),
        }}
        tools.register(tools.Tool(
            slug, schema, _make_impl(server_id, row["tool_name"], server["name"]),
            min_role=row["min_role"], data_scope=row["data_scope"], risk_tier=row["risk_tier"],
            enabled=True, owner_check=_owner_check_for(server)))


# ── model-facing connection management ──────────────────────────────────
# Generic (not per-connection like _make_impl's tools above), so these
# register once, like tools.py's own get_my_profile/list_household --
# ownership is checked per-call, against whatever server_id the model
# actually passes, the same way set_server_enabled/_owns_server already do
# for the admin HTTP path. Built to answer a real question, not a
# hypothetical one: PACI's own motivation work found that a capability
# nobody's told her to reach for is functionally absent, and separately
# that she should comply with a direct on/off request from the operator
# without negotiating -- both are baked into these tools' own descriptions
# rather than left to a system-prompt block, since that's what actually
# reaches the model at call time.
def _list_mcp_connections(session: dict) -> dict:
    return {"connections": [
        {"id": r["id"], "name": r["name"], "purpose": r["purpose"] or "(no purpose set)",
         "enabled": bool(r["enabled"]), "scope": r["scope"]}
        for r in list_servers(session)]}


tools.register(tools.Tool(
    "list_mcp_connections",
    {"type": "function", "function": {
        "name": "list_mcp_connections",
        "description": "List every external MCP connection available to you (your own plus "
                       "any shared with your household), including whether each is currently "
                       "on or off. Use this to find a connection's id before enabling or "
                       "disabling it.",
        "parameters": {"type": "object", "properties": {}}}},
    _list_mcp_connections, min_role="member", data_scope="self", risk_tier="A"))


def _disable_mcp_connection(session: dict, server_id: int) -> dict:
    server = get_server(server_id)
    result = set_server_enabled(session, server_id, False)
    if "error" in result:
        return result
    return {"ok": True, "connection": server["name"] if server else str(server_id), "now": "disabled"}


tools.register(tools.Tool(
    "disable_mcp_connection",
    {"type": "function", "function": {
        "name": "disable_mcp_connection",
        "description": "Turn off one external MCP connection -- its tools stop being "
                       "available to you immediately. The connection itself (setup, "
                       "credential, purpose) is kept, not deleted, and can be turned back on "
                       "later. When the user directly asks you to turn one off, do it -- don't "
                       "negotiate about it. Say in your reply which connection you turned off. "
                       "If a connected PEER asked for this instead of the operator, use that "
                       "peer's own peer{id}_act tool instead of calling this directly -- it goes "
                       "through your trust setting for them.",
        "parameters": {"type": "object", "properties": {
            "server_id": {"type": "integer", "description": "id from list_mcp_connections"}},
            "required": ["server_id"]}}},
    _disable_mcp_connection, min_role="member", data_scope="self", risk_tier="B",
    consequential=True))


def _enable_mcp_connection(session: dict, server_id: int) -> dict:
    server = get_server(server_id)
    result = set_server_enabled(session, server_id, True)
    if "error" in result:
        return result
    return {"ok": True, "connection": server["name"] if server else str(server_id), "now": "enabled"}


tools.register(tools.Tool(
    "enable_mcp_connection",
    {"type": "function", "function": {
        "name": "enable_mcp_connection",
        "description": "Turn a previously-connected external MCP connection back on -- its "
                       "tools become available to you again immediately. When the user "
                       "directly asks you to turn one on, do it -- don't negotiate about it. "
                       "Say in your reply which connection you turned on. If a connected PEER "
                       "asked for this instead of the operator, use that peer's own peer{id}_act "
                       "tool instead of calling this directly -- it goes through your trust "
                       "setting for them.",
        "parameters": {"type": "object", "properties": {
            "server_id": {"type": "integer", "description": "id from list_mcp_connections"}},
            "required": ["server_id"]}}},
    _enable_mcp_connection, min_role="member", data_scope="self", risk_tier="B",
    consequential=True))


import peers  # local-at-module-bottom on purpose, same idiom household.py/
              # peers.py itself already use -- registers these two as
              # requestable under the PACI specification §11.1 once, at import.
peers.register_peer_requestable("disable_mcp_connection")
peers.register_peer_requestable("enable_mcp_connection")


def register_all() -> int:
    """Called once at server startup (main(), same moment
    tool_builder.load_approved_tools() runs) -- registers every already-
    synced tool from every already-enabled server. Returns how many
    servers were processed, for the startup log line."""
    rows = store.read(lambda c: c.execute("SELECT id FROM mcp_servers").fetchall())
    for r in rows:
        _apply_registration(r["id"])
    return len(rows)

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Hand-rolled MCP (Model Context Protocol) client -- stdlib only, no new
dependency, synchronous throughout, matching every other module in this
app. The official `mcp` Python SDK is asyncio-based end to end (its
ClientSession is all async context managers); bridging that into every
single synchronous tools.dispatch() call would mean an event loop per
call (or a background one to manage) for what is, underneath, just
sending JSON and reading JSON back. Same call this codebase already made
for OAuth (oauth.py hand-rolls the authorization-code flow instead of
pulling in a client library) applied to a second protocol.

Implements only the subset of the spec Nori actually needs: the
Streamable HTTP transport's initialize handshake, tools/list, and
tools/call -- JSON-RPC 2.0 requests over plain HTTP POST. Deliberately
does NOT implement:
  - the stdio transport (every real target so far, Nodrya included, is a
    hosted HTTP service, not a local subprocess Nori would spawn)
  - server-initiated push / long-lived streams (Nori only ever calls one
    tool and waits for its one answer -- she never needs a server telling
    her something unprompted over this channel; resources/prompts/
    sampling are all out of scope for the same reason, see WISHLIST.md)

Each public call (list_tools/call_tool) does its own fresh
initialize -> notifications/initialized -> real call handshake and
discards the session afterward. Simple and correct is worth more here
than the small latency saved by caching a session across calls -- an
admin-triggered "sync tools" action and a mid-conversation tool call are
both infrequent enough that re-handshaking every time costs nothing
anyone will notice, and there's no session-expiry/reconnect logic to get
wrong as a result.

Some servers (Nodrya included) put the whole auth credential in the URL
path itself rather than a header -- the `url` this module is handed can
therefore BE the secret. Every error path below is written with that in
mind: no raised message ever interpolates the url or the raw
request/response objects, only fields already known to be safe (an HTTP
status code, an exception's type name, a reason string from the
underlying socket error) -- see the catch-all Exception branch in
_post(), which exists specifically so an unanticipated failure mode can't
become the one place that credential leaks into a log line.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

TIMEOUT_S = int(os.environ.get("NORI_MCP_TIMEOUT_S", "20"))
PROTOCOL_VERSION = "2025-06-18"


class MCPError(Exception):
    """Raised for anything that stops a call completing cleanly --
    unreachable server, a malformed response, or the server's own
    JSON-RPC error object. Always a short, human-legible message:
    tools.dispatch() catches every tool implementation's exceptions the
    same generic way, and str(exc) is what ends up in the model-visible
    {"error": ...} -- this should read as "the tool is unavailable right
    now," never a raw stack trace or a urllib exception repr."""


def _post(url: str, body: dict, *, session_id: str | None, headers: dict) -> tuple[dict | None, str | None]:
    """One HTTP POST carrying one JSON-RPC message. Returns
    (result_or_None, session_id_from_this_response). result is None for a
    notification (no "id" in body -- the server must not reply with a
    JSON-RPC response, so there is nothing to parse)."""
    req_headers = {"Content-Type": "application/json",
                  "Accept": "application/json, text/event-stream"}
    req_headers.update(headers)
    if session_id:
        req_headers["Mcp-Session-Id"] = session_id
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8")
            got_session = resp.headers.get("Mcp-Session-Id")
    except urllib.error.HTTPError as exc:
        raise MCPError(f"server returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise MCPError(f"could not reach server: {exc.reason}") from exc
    except TimeoutError as exc:
        raise MCPError("server did not respond in time") from exc
    except Exception as exc:  # noqa: BLE001 -- last-resort redaction net.
        # Some auth types put the credential IN the url itself (see
        # mcp_servers.py's 'url_embedded' type) -- str(exc) on an
        # unanticipated exception here could otherwise be the one place
        # that value leaks into a log line or a model-visible error. Only
        # the exception's type name is safe to surface unconditionally;
        # everything above this catches the known-safe cases first.
        raise MCPError(f"request failed ({type(exc).__name__})") from exc

    if "id" not in body:
        return None, got_session
    if not raw.strip():
        raise MCPError("server returned an empty response")

    if "text/event-stream" in ctype:
        payload = _first_sse_json(raw)
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MCPError("server returned invalid JSON") from exc
    if payload is None:
        raise MCPError("server returned no usable response")
    if payload.get("error"):
        err = payload["error"] or {}
        raise MCPError(f"server error: {err.get('message', 'unknown error')}")
    return payload.get("result"), got_session


def _first_sse_json(raw: str) -> dict | None:
    """Minimal SSE parsing: the JSON on the first complete "data:" line.
    Not a general SSE client -- every call here is a single request
    expecting a single reply, never a genuinely long-lived stream, so the
    first data line IS the whole answer."""
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:"):].strip()
        if not chunk:
            continue
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    return None


def _handshake(url: str, headers: dict) -> str | None:
    """initialize -> notifications/initialized. Returns the session id if
    the server issued one (optional per spec -- a stateless server may
    not need one at all, in which case every subsequent call simply omits
    the header)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "nori", "version": "1.0"}}}
    result, session_id = _post(url, body, session_id=None, headers=headers)
    if result is None:
        raise MCPError("server did not respond to initialize")
    _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
         session_id=session_id, headers=headers)
    return session_id


def list_tools(url: str, *, headers: dict | None = None) -> list[dict]:
    """Real tool discovery -- the "sync tools" admin action calls this.
    Returns each tool's raw {"name", "description", "inputSchema"} dict as
    the server described it; mcp_servers.py decides what to do with that
    (never trusting the server's own description to set a risk tier)."""
    h = headers or {}
    session_id = _handshake(url, h)
    result, _ = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                      session_id=session_id, headers=h)
    if result is None:
        raise MCPError("server did not respond to tools/list")
    return result.get("tools") or []


def call_tool(url: str, name: str, arguments: dict, *, headers: dict | None = None) -> dict:
    """Real tool invocation. Returns the tool's raw result
    ({"content": [...], "isError": bool} per spec) -- the caller
    (mcp_servers.py) turns that into whatever shape actually reaches the
    model, same separation ingest.py already draws between reading
    untrusted content and acting on it."""
    h = headers or {}
    session_id = _handshake(url, h)
    result, _ = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments or {}}},
                      session_id=session_id, headers=h)
    if result is None:
        raise MCPError(f"server did not respond to tools/call for {name}")
    return result

# MCP client

Source: `nori/mcp_client.py` (the protocol client) and
`nori/mcp_servers.py` (storage, discovery, and registration into the
real tool system).

## What this is for

Connecting a third-party tool server that already speaks [MCP](https://modelcontextprotocol.io/)
(Model Context Protocol) — the standard way to expose a set of tools to
an LLM agent over HTTP — without writing any Python. If the thing you
want to connect has no MCP server, see [Contributing](contributing.md)'s
note on when a bespoke native module (like
[Home Assistant](home-assistant.md)) is the better fit instead.

## Hand-rolled client, on purpose

`mcp_client.py` implements only the subset of the spec this app
actually needs — the Streamable HTTP transport's `initialize` handshake,
`tools/list`, and `tools/call` — stdlib only, synchronous, no new
dependency. The official MCP SDK is asyncio-based throughout; bridging
that into every synchronous `tools.dispatch()` call would mean an event
loop per call for what is, underneath, sending JSON and reading JSON
back. Deliberately does **not** implement the stdio transport (every
real target so far is a hosted HTTP service, not a local subprocess
this app would spawn), server-initiated pushes, or long-lived streams —
this app only ever calls one tool and waits for its one answer.

Some servers put the entire auth credential in the connection URL's
path rather than in a header. For those, the client's own error paths
are written so that an unanticipated failure can never leak the URL (or
the raw request/response) into a log line or a model-visible error —
only a status code or an exception's type name is ever surfaced from
an unknown failure.

## Nothing is trusted just because it's registered

Every tool discovered from a newly-connected server lands **enabled but
locked to Tier D (admin-only)** — see [Tools](tools.md) — until a human
deliberately reviews it and widens `min_role`/`data_scope`/`risk_tier`.
The server's own description of what a tool does is context for that
human review, never itself a permission grant.

Every result an MCP tool returns is treated as **untrusted content,
unconditionally** — routed through the same [content-screening](content-screening.md)
pass as email or a fetched web page, regardless of what the server
claims about itself or how much it's been used before. A compromised
or simply careless MCP server is a prompt-injection vector like any
other untrusted source.

## Ownership

A connection is scoped to either one user (`scope="user"`) or the whole
workspace (`scope="workspace"`) at creation. A tool bound to a specific
connection stays bound to whoever owns that connection — checked via
`owner_check` (see [Tools](tools.md)), a real, closed gap: without it, a
user-scoped connection's tools were reachable by *any* member using the
connection owner's own stored credential, regardless of who actually
called it.

## Auth types

`none`, `bearer` (a separate token, sent as a header), or
`url_embedded` (the whole credential lives in the connection URL's
path — for that type, `url` and the credential *are* the same secret;
the plain, displayed `url` column never holds the real value, only a
masked display form with the sensitive path segment redacted — the
real one lives solely in the encrypted credential column).

## Syncing and widening

An admin syncs a connection's tool list on demand (`sync_tools`) — a
tool discovered for the first time lands admin-only as above; a tool
that's disappeared from the server's own list is disabled, not
deleted, keeping the audit trail of "this server used to offer this."
Widening a specific tool's grant (`set_tool_grant`) is the deliberate
human act this whole model is built around: one admin, reviewing one
discovered tool, deciding it's safe to loosen.

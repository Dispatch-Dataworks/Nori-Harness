# Architecture

The shape of the whole app, so the per-system pages make sense in
context. Read this once; the system pages assume it.

## What Nori actually is

A single Python process (stdlib `http.server`, no framework — see
`server.py`), one SQLite database (`nori.db`, see [Settings
model](settings.md) and below), and a background thread that ticks once
a minute. No queue, no worker pool, no external services required to
start (Google/Microsoft/Home Assistant/Tavily/a peer agent are all
optional, added later). Multi-user: one workspace (the household), each
member their own account and role (see [Auth & roles](auth-and-roles.md)).

## Request lifecycle

1. `Handler` (in `server.py`, a `BaseHTTPRequestHandler` subclass)
   receives the HTTP request. `do_GET`/`do_POST` route on the exact
   path, in a long, explicit if-chain — there's no framework-level
   router, no path-parameter magic, just string comparisons, on purpose:
   grep-able, no indirection to trace through.
2. A session cookie is resolved to a real session row (`accounts.py`);
   CSRF is checked on every POST. Every authenticated page/action reads
   `sess["user_id"]`/`sess["workspace_id"]`/`sess["role"]` from that
   session, never from a request parameter — see [Auth & roles](auth-and-roles.md)
   and [Tools & the dispatch model](tools.md) for why that specific
   detail is load-bearing.
3. For `/send` (a real chat message) or the equivalent proactive/peer/
   scheduled trigger: `turns.run()` acquires a per-user lock, then calls
   `chat.run()` — see **Turn locking** and **The turn loop** below.
4. The response is a plain HTML page (server-rendered, no client-side
   framework — see `page_app()`/`page()`) or a small JSON body for the
   few endpoints that need one (`/send`, `/poll`, `/retry`).

## Turn locking (`turns.py`)

Nori is multi-user, so locking is **per user_id**, not global — two
different household members' turns run fully in parallel; only the same
user's two requests (two browser tabs, or a tab racing a proactive ping)
ever contend. The real bug this fixes: two tabs sending ~0.5s apart used
to both reach the model and both post a reply to what the user
experienced as one conversation.

Mechanism: whoever gets the per-user lock runs the real turn
(`first_run`); anyone else for that same user who calls in while it's
held gets `{"queued": True}` back immediately (never blocks) — safe
because their message was already written to the database *before* the
call, so nothing is lost, only delayed. Once its own turn finishes, the
lock holder sweeps for any message that arrived after its own starting
snapshot and answers that too (`sweep_run`), up to 5 rounds, before
releasing — so the second tab gets a real reply without a second
concurrent model call ever starting. Source: `nori/turns.py` in full,
it's under 90 lines.

## The turn loop (`chat.py`)

One turn = one or more model calls. `chat.run()` builds the message
list ([Context & tuning](context.md) covers what's actually in it),
calls the model, and if the reply is a tool call, dispatches it through
`tools.dispatch()` — see [Tools & the dispatch model](tools.md) for what
that single chokepoint actually enforces — appends the result, and
calls the model again. This repeats until a plain-text reply or a round
limit (`max_rounds`, per-user-tunable, lower for an unattended turn like
a proactive ping than for a live chat someone's watching — nobody's
there to notice a runaway unattended loop).

A live chat turn, a proactive ping, a scheduled-task firing, a reminder
nag, and a peer-motivated turn are all the *same* `chat.run()` call with
different arguments (`extra_message`, `max_rounds`,
`include_peer_pending`) — there's deliberately no second turn-running
code path anywhere in this app.

## How the prompt is assembled

`context.py`'s `_system_parts()` is the one place the standing system
prompt's own section order is decided — every other function that needs
to describe or measure it (the live prompt itself, and the [context
composition breakdown](context.md) on the context-tuning settings page)
derives from that same list rather than each keeping its own copy. In
order: a short name/time header, her **persona** (character content,
[editable](setup.md#customizing-her)), **tool-usage guidance**
(mechanics — kept deliberately separate from persona), her current **emotional state** line ([Emotions](emotions.md)),
the always-loaded **memory** slice ([Memory](memory.md)), a sub-agent
**jobs digest**, a capability block naming connected **MCP servers**
([MCP client](mcp.md)) and **peer agents** ([Peer agents](peer-agents.md)),
and recent peer exchanges.

Two things are deliberately *not* in that standing block, on the same
"proximity is what makes content actually get used" principle:
per-turn nudges (the emotion precheck line, a duration-tracker digest —
see [Emotions](emotions.md)) are injected as the *last* message before
the model replies, not buried mid-prompt; and pending peer content
travels as its own late message per peer, not string-concatenated into
the standing prompt. Both were real, measured fixes, not stylistic
preferences — see [Emotions](emotions.md) for the actual before/after.

After the system prompt: any **compacted** older sessions
([Compaction](compaction.md)), oldest first, then the live raw message
window, oldest first — one continuous chronological sequence, most
recent last.

## Storage

One SQLite file (`nori.db`, in the configured data directory — see
[Deployment](deployment-and-watchdog.md)). `store.py` owns the schema
and is the *only* module allowed to run raw SQL against most tables —
every other module goes through a narrow, named function
(`conversation.add_message()`, not a bare `INSERT`) so a table's shape
can change in one place. Migrations are plain `ALTER TABLE` statements
in a list, applied at startup, each one safe to re-run (a "column
already exists" error is swallowed) — there's no migration framework,
no down-migrations; this is a single-operator, single-database app, not
a multi-tenant service that needs schema versioning machinery.

Secrets at rest (an OAuth refresh token, a peer's shared secret) are
Fernet-encrypted with a key kept **outside** the database
(`secret.key`, next to it on disk) — see [Auth & roles](auth-and-roles.md#secrets-at-rest).
Backups encrypt the whole archive with a *second*, separate key, for a
reason explained in [Backups](backups.md).

## Instrumentation

`timing.py` gives every turn and every HTTP request a real, structured
per-stage timing line (model call, tool dispatch, context build...) when
`debug_timing_enabled` is on ([Settings model](settings.md)) — one
`TIMING {...}` JSON line per turn in the log, not a metrics service.
Off by default; negligible cost either way (one `SELECT` when off).

## Module map

Roughly: `server.py` is the whole web layer (every page, every route).
`chat.py`/`turns.py`/`context.py`/`conversation.py` are the turn engine.
`store.py`/`config.py`/`crypto.py` are storage/settings/secrets.
`own_output.py` scrubs her own past words before they are shown back to her. `persona_admin.py` is the persona editor page. `accounts.py` is auth. `tools.py`/`capabilities.py`/`tool_builder.py`/
`self_knowledge.py` are the tool system. Everything else — `memory.py`,
`compaction.py`, `emotion.py`, `homeassistant.py`, `peers.py`,
`scheduler.py`, `tasks.py`/`notes.py`/`reminders.py`/`trackers.py`,
`voice.py`, `imagegen.py`, `backup.py`, `integration_health.py`, and the
Google/Microsoft/MCP/web-search modules — is one feature area each,
documented on its own page (see [the index](README.md)).

One module worth naming explicitly because it's *not* for an operator:
`agent_channel.py` is a break-glass, out-of-band way to ask Nori
something directly during development, off the record, no real
conversation turn. It's how some of her own design details (phrasing,
categorization) got decided by asking her directly rather than guessing
on her behalf. Not something a running household ever needs to touch.

# Tools & the dispatch model

Source: `nori/tools.py` (the registry and the one enforcement point),
`nori/capabilities.py` (what a given session actually sees),
`nori/tool_builder.py` (building a tool without a code deploy), and
`nori/self_knowledge.py` (`explain_self` — how she looks any of this
up herself instead of guessing).

## The registry

Every tool — built in, from a connected MCP server, or offered by a
connected peer — is a `tools.Tool(name, schema, impl, min_role,
data_scope, risk_tier, enabled, owner_check)` registered once, at
import time, into one process-wide dict. There is no second way to
register a callable capability.

- **`min_role`** — `"admin"` or `"member"`. A member-role session never
  even sees an admin-only tool in its own schema list
  (`active_schemas()`) — not just refused if attempted, genuinely
  absent from what the model is told exists.
- **`data_scope`** — `"self"`, `"workspace"`, or `"system"`, metadata
  describing what a tool touches. This is *not* an independent runtime
  gate — every native tool is self-scoping by construction (its own
  implementation reads `session["user_id"]`/`["workspace_id"]` to know
  what to act on), so nothing in `dispatch()` needs to check this
  separately. The one real exception is `owner_check` below.
- **`risk_tier`** — `"A"`/`"B"`/`"C"`/`"D"`, in practice: roughly
  read-only/self-scoped (A), member-callable with a real effect (B),
  admin-only where a member equivalent could plausibly exist but was
  deliberately not widened (C), and system-level (D). **Only D's
  meaning is actually enforced in code**: `risk_tier="D"` is checked at
  *registration time* and requires `min_role="admin"` — a hard floor,
  not a default, and not something a settings toggle can change. A/B/C
  are a real, consistently-applied convention across every tool in this
  codebase, but nothing currently *enforces* that a "B" tool couldn't
  be registered member-callable with a workspace-wide destructive
  effect — see [The enforcement model](enforcement-model.md) for why
  that distinction (code-enforced vs. convention) is worth tracking
  explicitly rather than assuming from the letter grade.
- **`owner_check`** — an optional extra gate, checked by both
  `dispatch()` and `active_schemas()`, for the one real case
  `min_role`/`data_scope` can't express: a tool whose underlying
  resource is fixed at *registration* time rather than derived from
  the calling session — an MCP tool bound to one specific connected
  server, which itself belongs to one specific user or workspace.
  Without this, any member could have called another member's private
  MCP connection's tools using the *connection owner's* stored
  credential — a real gap this closed, not a hypothetical one.

## The one enforcement point

`tools.dispatch(name, args, session)` is the *only* place a tool call
actually runs — see [Architecture](architecture.md)'s turn loop and
[The enforcement model](enforcement-model.md). On every call it: looks
up the tool (an unknown or disabled name is a plain error, never a
crash), checks `min_role` against the session's real role, checks
`owner_check` if one is set, rate-limits per user per tool, strips any
`user_id`/`workspace_id` the model tried to pass as an argument (belt
and suspenders — no tool schema is allowed to declare either
parameter in the first place), and calls the real implementation with
the session it was actually given. An implementation's own exception
becomes a plain `{"error": ...}` result, never a crash that takes the
turn down.

## What she can actually see (`capabilities.gather`)

The tool *registry* is process-wide; what one session's turn is
actually offered is scoped live, every time, by `capabilities.gather(session)` —
built-in tools filtered by role, MCP tools grouped by connection with
each one's real enabled/purpose state, peer-offered tools the same way.
`active_tools_page` (Settings → Active tools, every role) renders this
directly — "disabled but configured" is shown as such, not silently
missing, on purpose: a tool that's real but currently unreachable is a
different, useful fact from one that doesn't exist.

## Building a new tool without a deploy (the tool builder)

Admin-only, in-app pipeline for a tool that doesn't need repo access:
**draft** → a **static safety check** → a **sandboxed dry run** (zero
real-resource reach, shown to the admin as part of review) → **approve**
(a typed confirmation phrase, versioned, append-only) → **enable** (a
separate circuit-breaker flag from approval) → **widen** to every
member (a separate, explicit, never-automatic step — editing an
approved tool's code resets its approval, so a change can't quietly
ship without a fresh review). A newly-approved-and-enabled tool needs a
**server restart** to actually go live — there's no hot-reload here,
deliberately: a tool that can execute real code shouldn't become
callable mid-process without a deliberate restart.

## `explain_self` — looking this up instead of guessing

A member-level tool whose entire purpose is described in
[the confabulation pattern](confabulation.md): rather than guess about
her own capabilities, `explain_self(topic=...)` reads live from the
real source for whichever topic is asked — her real tool list
(`tools`), typed memory (`memory`), compaction (`compaction`), PACI and
her own peer connections (`paci`), the sub-agent roster
(`sub_agents`), and her own real address plus every real page on the
instance (`identity`). Every answer is generated fresh from the same
source a human would check, not a second, hand-written copy that could
say something different from what's actually true.

# Home Assistant

Source: `nori/homeassistant.py`, settings at `/admin/homeassistant`.

## Native, not MCP

Home Assistant has an ordinary REST API, but the integration is a
bespoke module rather than something registered through the
[MCP client](mcp.md). The real requirements here — entity discovery
kept structurally separate from what she can actually see, per-entity
peer reachability, one chokepoint for logging and screening — have no
way to be expressed by "a third-party server's tools get discovered
generically and locked down until reviewed," which is what the MCP
model is for. Bespoke code was needed regardless of transport, so
going fully native (one module owning the whole path, the same
discipline [web search & fetch](web-search-and-fetch.md) already
established) was simpler than bending the MCP model to fit.

## Setup

Set `HOME_ASSISTANT_URL` and `HOME_ASSISTANT_API_KEY` in `nori.env`
(a long-lived access token from your HA profile) and restart. Admin
opens **settings → Home Assistant** to run discovery — a one-time (or
re-run-on-demand) pull of every entity HA reports via `/api/states`.

## Discovery and exposure are two different things, structurally

Discovery (admin-only) upserts HA's full entity list into a local
table — domain, friendly name, last-seen timestamp, raw state — every
existing entity is left with its exposure flags untouched (a
rediscovery never silently re-exposes or un-exposes anything an admin
already decided), and a newly-seen one starts fully unexposed.

**No function in this module hands the model-facing side that full
discovered set — not even by accident.** Two flags per entity, set
only from the admin exposure page:

- **`enabled`** — she can see and use this entity, full stop. No
  separate read/write split for her own access.
- **`enabled_for_peers`** — a connected [peer agent](peer-agents.md)
  can also reach it, gated further by that peer's own trust level
  (prompt/full — the same trust ladder every other peer-requestable
  action uses). Turning this on forces `enabled` on too — checked in
  code, not only relied on in the admin page's UI — a peer reaching
  something she can't see herself would be incoherent.

There is deliberately no device-class-based policy anywhere — a lock
and a light go through the identical gate, the identical control tool,
the identical peer trust ladder. Which entities are sensitive is the
operator's own call, made per entity on the exposure page, not a
severity scheme this app second-guesses by domain.

## What the tools can do

- `ha_list_entities` — the exposed roster, nothing else.
- `ha_get_state` — one call against HA's `/api/states` regardless of
  how many entity_ids are asked for, filtered to the exposed set
  locally; anything requested that isn't exposed is dropped from the
  request entirely, never merely withheld from the answer. An entity
  that was exposed but has since vanished from HA (removed, renamed,
  unpaired) comes back as an explicit `missing_entities` entry rather
  than silently omitted — see [Confabulation](confabulation.md) for
  why that distinction matters.
- `ha_control` — turn things on/off, lock/unlock, open/close, set a
  value, run a script, and so on, through a fixed action→HA-service
  map that only exposes actions with one unambiguous meaning (an
  automation can be enabled/disabled through this tool, never
  *triggered* — that fires its real actions immediately and needs more
  thought than a blanket allow). A light's color/brightness parameters
  are checked against that specific entity's own reported
  `supported_color_modes` before the request is ever sent, and refused
  outright — not silently dropped — on a mismatch.
- `ha_get_history` — a condensed sequence of real state *transitions*
  over a recent window, not every raw poll (a frequently-reporting
  device can otherwise mean hundreds of near-duplicate entries for a
  handful of real changes). For a location tracker, a transition is
  also emitted on a real move past a fixed distance threshold even if
  the state string itself didn't change, so a long excursion isn't
  collapsed to a single stale coordinate.

All three per-entity tools share one gate: an entity not currently
exposed (to her, or to peers when the call is a peer's own) is refused
before Home Assistant is ever contacted.

## Live state is treated as untrusted content

Entity state comes from devices, not from anyone in the household —
every state or history payload passes through the same
[content-screening](content-screening.md) pass used for email or a
fetched web page before it can reach a tool-capable turn. This does
**not** apply to the exposed roster's own names — an admin already
reviewed those at the moment they checked the box.

## Honest failure, not a guess

A wrong or expired API key, an unreachable HA instance, and a control
call against an entity that's disappeared from HA each produce a
distinct, specific reason string — not a bare error code or (worse) a
plausible-sounding invented explanation. HA's own control endpoint
returns an identical empty response for a real success, a no-op, and
a call against a nonexistent entity; this module resolves that by
checking the HTTP status for whether the call itself succeeded and, on
top of that, actually re-reading certain lights back after a call
known to silently no-op some brightness/color changes on real
hardware — verified live against real devices, documented in-line in
the source rather than assumed from HA's own docs.

## Extending it

A new device domain needs an entry in the `_SERVICES` map (action name
→ real HA service, verified against HA's own service registry, not
guessed) — see [Contributing](contributing.md). Anything that reads
live device data still has to go through `_screen_state()`; anything
peer-reachable still has to register through `register_peer_actions()`
and pick a trust level deliberately, not default to the most
permissive one.

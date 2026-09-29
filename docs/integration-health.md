# Integration health

Source: `nori/integration_health.py`.

## The same idea as PACI's health check, extended to everything else

[Peer agents](peer-agents.md#health-and-what-connected-actually-means)
covers PACI's own liveness check for a peer channel; this module
applies the identical idea to every other outward-facing connection
this app has — Home Assistant, Tavily, each connected Google/Microsoft
account, each connected MCP server. Same posture: a cheap
connectivity/auth probe, never a model call, run on a timer. The
motivation is identical too — a channel that fails silently is worse
than one that fails loudly, and "she stated a confident but wrong
reason something wasn't working" is exactly the failure this closes.
See [Confabulation](confabulation.md).

## A wider vocabulary than PACI's, because these fail more specifically

`healthy`, `not_configured` (not a failure — the operator simply
hasn't set this one up), `unreachable`, `auth_expired` (a token that
used to work has stopped), `scope_missing` (authenticated fine,
refused for lacking a permission), `api_disabled` (authenticated fine,
refused because the provider's API itself isn't enabled), `rate_limited`,
`error` (anything else — always with the real detail attached, never
collapsed to a bare "something's wrong"), and `unknown` (configured,
but never actually live-checked yet).

## Two different strings for two different audiences

Every check returns both a `detail` and an `explain`:

- **`detail`** is the raw, technical text a stranger's admin could act
  on — "web search failed (401): Unauthorized," not "search is
  broken."
- **`explain`** is a separate, short line in her own voice, meant for
  her to relay directly to the person asking — "your Outlook
  connection expired, reconnect it" rather than "I can't reach your
  email." This is the direct antidote to a real, tracked incident: she
  once told the operator a photo hadn't arrived when the true answer
  was a setting he'd simply never turned on. Given a real, specific
  reason instead of a bare status code, there's no gap left for her to
  fill with an invented one.

`explain` is written in her own agent register deliberately, not
softened — she names `.env`, OAuth scopes, and settings pages
directly, the same way she'd reason about her own machinery out loud.
This is a choice specific to her own persona's design; the equivalent
choice for the other self-hosted persona app in this repository runs
the opposite direction (never breaking frame to talk about its own
implementation) — two different personas, two different registers,
deliberately not reconciled to match.

## Cost is a real, separate axis from latency

Home Assistant, Google/Microsoft, and MCP probes are free, metadata-
only reads against generous quotas — cheap enough to run on a timer
without a second thought. **Tavily is not**: its only real endpoint
*is* a paid search call, so an automatic live probe every few minutes
would quietly spend a stranger's search-credit budget on operational
overhead they never asked for. Tavily's scheduled check is therefore
configuration-only (is a key present at all — free to check) — the
actual live "does this key work" probe only runs on demand, from an
explicit "check now" in the admin page. Until someone runs that,
Tavily reads `unknown`, never a guessed `healthy`.

## Scope: the primary admin's own connections

`connected_accounts` rows are per-user, but every OAuth setup this
app documents ([Google & Microsoft](google-and-microsoft.md)) assumes
one operator doing the connecting — this sweep checks that primary
admin's own connections, the same "first admin" convention
[backups](backups.md)' own daily run uses, rather than a
per-household-member fan-out nobody asked for. Home Assistant, Tavily,
and MCP servers are instance-wide already, with no per-user dimension
to sweep across at all.

## Where it runs

`tick()` is called from the same background cycle
[Scheduler](scheduler.md) already ticks everything else from — no
second timer.

# Web search & fetch

Source: `nori/webtools.py`, settings at `/admin/webtools`
(`server.py`'s `webtools_admin_form`).

## Two tools

- **`web_search`** — a live web search for something not already in
  [memory](memory.md) or the conversation. A direct call to
  [Tavily](https://tavily.com)'s search API — no browser, no scraping,
  no third-party SDK.
- **`web_fetch`** — reads one specific URL's actual content (a search
  result, a link a household member shared). This app's own hardened
  fetch, not Tavily's — see "SSRF protection" below.

Both are on by default and ordinary tools she reaches for on her own
judgment — nothing to invoke manually. `web_search` is inert without a
key (see below); `web_fetch` needs no key at all, since it's this
app's own code hitting the target directly.

## Setting up `web_search`: a Tavily key

1. Sign up at [app.tavily.com](https://app.tavily.com) — free tier is
   1,000 credits/month, no card required.
2. Copy the API key from the dashboard (starts with `tvly-`).
3. Set `TAVILY_API_KEY=<key>` in `nori.env` (see [Setup](setup.md#environment-variables-env)
   for where that file actually lives).
4. Restart — env vars are read once at process start, same as every
   other key in that file.

Sign in as admin and open **settings → Web search/fetch**
(`/admin/webtools`) to verify: the status line reads either "Tavily
key set," or "no Tavily key yet — `web_search` will say so plainly,
not fail obscurely" if it isn't configured. If it's still unconfigured
after a restart, check for a stray quote or space around the value —
no quotes needed, just the raw value after `=`.

## The settings pane

Admin-only, workspace-wide (one shared configuration for the whole
household, not per-member — see [Settings model](settings.md)).

**Read blacklist** — GET requests are open by default; `web_fetch` can
read any ordinary public page unless a domain (or `*.example.com`
wildcard, which also blocks the bare domain) is added here. This is an
exception list, not an allow list — the common case needs nothing
added.

**Write allow-list** — anything other than GET (POST/PUT/PATCH/DELETE)
is closed by default, the opposite posture on purpose: a write to a
third party is a meaningfully bigger risk than a read (data leaving
the household, a real side effect on someone else's site), so a
domain must be explicitly added before she can write to it at all.

**Write mode — off / simulated / real** — a second, independent gate
specifically on writes:

- **off** (default) — every write is refused before the hostname is
  even resolved.
- **simulated** — every real check still runs (the allow-list, the
  SSRF check) exactly as it would for real, but nothing is ever
  actually sent. The model is deliberately not told a write was
  simulated — from her side it looks like an ordinary failed
  connection, so what you observe is genuine behavior, not something
  adjusted for knowing it's a test. **The settings-page log is the
  only place the truth lives** — every simulated attempt is marked
  there in full, regardless of what she reports happened. Repeated
  simulated attempts at the same endpoint within a 15-minute window
  fast-fail after the third try, with a varied failure message each
  time, rather than a suspiciously identical response every attempt.
- **real** — an allowed write actually goes out.

You cannot jump from off straight to real — the setting enforces
simulated as a mandatory first step, so turning writes on for real is
always two deliberate saves, never one.

**The request log** — every call, allowed or denied, real or
simulated: method, which household member's turn it ran under, the
URL, which specific rule denied it when denied (blacklist, allow-list,
the SSRF check, an unsupported method, write mode off), the real HTTP
status when one exists, and the payload on a write. A complete log,
not a sample.

## SSRF protection

`web_fetch` resolves every hostname and refuses to connect if any
resolved address is private, loopback, link-local, or otherwise
non-public (`_is_public_ip`) — so it can't reach anything on your own
network, even indirectly through a redirect: each redirect hop gets
the identical check, not just the first one. Not configurable, on
purpose — this is a real, code-enforced floor (see [The enforcement
model](enforcement-model.md)); the blacklist/allow-list above are the
only per-domain controls, layered on top of this, not instead of it.

## What untrusted content means here

Anything `web_search` or `web_fetch` returns is text from the open
internet, not something this app wrote or vetted — it goes through the
same [content-screening](content-screening.md) discipline as any other
external source before it can influence a tool-capable turn.

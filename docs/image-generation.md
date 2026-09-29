# Image generation

Source: `nori/imagegen.py`, spend accounting in `nori/medialog.py`.

## Two tools, one shared budget

- **`generate_image_selfie`** — always anchored to her own face, using
  the same fixed reference image every avatar was generated from. For
  "send me a selfie" or "what does it look like where you are" —
  anything that should recognizably be her.
- **`imagine_image`** — general-purpose: any scene, with an optional
  reference image (a file already in her own [working folder](working-folder.md))
  for when the picture isn't of her at all.

Both draw from the **same** household daily budget
(`image_daily_cap_usd`, [Settings](settings.md)) rather than two
separate caps — splitting the budget in two would just mean whichever
tool happens to get called first silently determines what's left for
the other, a worse rule than sharing one. Spend is computed live from
the actual generation log (`medialog.spend_status()`), never a
maintained running counter that could drift from reality. Going over
the cap produces a plain refusal, not an error — the household simply
can't generate another image today until it resets.

## Generation model and provider

Runs through `chat.openrouter_image()` — OpenRouter, the same
provider path as every other model call this app makes, using
whichever model is configured (`image_model` in Settings; nothing
generates if it's left blank or if `image_gen_enabled` is off).

## Prompt assembly is fixed, regardless of what she writes

Every generation request wraps her own scene description between a
configured style prefix and a clothing constraint:
`<style prefix> <her prompt> <clothing constraint>`. The constraint is
appended after whatever she writes and is surfaced to her identically
in the tool's own guidance, so a refusal traceable to it isn't a
mystery she has to guess at.

## Staying within the provider's own policy

`prompt_flags()` is a cheap, local pre-check against the real image
provider's blocked-content categories, run *before* spending anything
on a call that would obviously be refused. This is compliance with the
provider's own policy, not an attempt to route around it —
`clean_image_error()` turns a genuine provider-side refusal into a
plain, actionable message instead of raw HTTP/JSON, while the full
original error is still logged in full to `medialog` for anyone
reviewing generation history.

Admins can turn **image content pre-check** off under **Settings → Images**
(`/settings?tab=media`) to skip the local keyword check for both tools.
The household setting `image_content_precheck_enabled` defaults to on
and takes effect immediately after saving. The prompt's content constraint,
provider-side checks, and shared image budget still apply.

## What's visible in the chat

The tool-call line itself (`used generate_image_selfie`, `used
imagine_image`) is filtered from the conversation *view* only — the
same display-only treatment applied to `set_emotion` and
`message_user` elsewhere. The generated image message itself is never
suppressed; hiding the mechanical tool-call line just avoids narrating
"I used a tool" on top of the actual result sitting right there.

## Timeout, and what happens when generation runs long

`chat.IMAGE_TIMEOUT_S` (default 120s, `NORI_IMAGE_TIMEOUT_S` overrides)
bounds the whole `urlopen()` call to the image provider — separate from
the ordinary chat-completion timeout, because real generation legitimately
takes far longer than a text reply. Raised from a hardcoded 60s
(2026-09-17) after real `.nori.log` `TIMING` data showed genuine
successes reaching 54.2s and 27% of real calls already hitting the old
60s ceiling and failing — not a guess, and not the 2026-09-13
IPv6/DNS-latency incident recurring (`imagegen.py` makes no network call
of its own; every request goes through `chat.openrouter_image()`, which
already inherits `server.py`'s process-wide IPv4-only `getaddrinfo`
patch like everything else).

A real timeout is a **distinguishable** failure, not a generic one:
`chat.openrouter_image()` tags it with `"timed out"` in the reason
string, and `clean_image_error()` checks for that specifically, *before*
the content-refusal check — so a slow provider can never read to her as
a policy refusal, and the message she gets explicitly tells her to say
it timed out rather than claim it worked. The full raw reason is still
logged to `medialog` either way, whether it's a timeout, a refusal, or
anything else.

**This call runs inside the same per-user turn lock (`turns.py`) every
other tool call does** — there is no special-cased async path for
image generation. A slow generation call genuinely holds that lock for
its own duration, the same way a slow MCP tool call would. This is not
a gap specific to image generation: `turns.py`'s own queue-not-drop
design (see [Peer agents](peer-agents.md#busy-means-queued-not-dropped))
already means a `reply_requested` compulsion or a second message from
the operator arriving mid-generation is deferred, never dropped —
confirmed with a real held lock in `tests/test_nori_image_timeout.py`,
not assumed. Raising the timeout makes that deferral proportionally
longer in the worst case (up to the new 120s, versus the old 60s) —
worth knowing if a household relies on fast peer turnaround, but not a
new failure mode. Decoupling image generation from the turn lock
entirely (an async job, delivered in a follow-up turn, the way
[sub-agents](sub-agents.md) already work) would remove this proportionality
outright — a real option, not built here since it changes when and how
the image actually appears in chat, which wasn't asked for.

## Extending it

A different image provider or a second reference-image mechanism
(per-user, selectable, the way this module's counterpart in the
other, separate self-hosted app in this repository offers) is a real
design decision, not a drop-in swap: see [Contributing](contributing.md),
and note that Nori's own single fixed reference (she has exactly one
face) is a deliberate simplification, not a limitation to work around
quietly.

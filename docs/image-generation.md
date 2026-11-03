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

Runs through `chat.generate_image()`, which sends the request to whichever connected
provider **Settings → Images → image provider** (`image_provider`) names:

| Provider | Endpoint | Reference picture | Notes |
| --- | --- | --- | --- |
| OpenRouter (default) | `/api/v1/images` | `input_references` | the connected OpenRouter provider's key, else `OPENROUTER_API_KEY` (`chat.openrouter_key()`); reports a real cost |
| OpenAI (API key) | `/v1/images/generations`, or multipart `/v1/images/edits` (`image[]`) when there is a reference | yes, via edits | model e.g. `gpt-image-1`; the organisation must be verified for image models; no cost is returned, so the per-image estimate is used |
| xAI (API key) | `/v1/images/generations`, or `/v1/images/edits` with a data-URL `image` | yes, via edits | model e.g. `grok-imagine-image-2.0`; asks for base64 and accepts a URL reply |
| Grok sign-in (`xai_oauth`) | the same api.x.ai endpoints, with the sign-in's OAuth token | yes, via edits | best-effort (2026-11-03): the token carries `api:access`, but xAI doesn't document image calls with it |
| ChatGPT sign-in (`openai_oauth`) | the Codex Responses endpoint with the `image_generation` tool (`chat._codex_image`) | yes, as `input_image` parts | best-effort (2026-11-03), undocumented; the "image model" is the **chat model** that runs the tool, by default the first one set up for that sign-in; no cost is returned |

Claude (Anthropic) and GitHub Copilot have no image generation, so they are not offered. The
two sign-ins are wired in on a best-effort basis. A refusal comes back in the provider's own
words in Settings → Images → recent activity, the same as any other image failure. A
model name only means something at its own provider, so choosing a provider resets the model
to that provider's default unless you also edit it (`imagegen.configure`). The chosen
provider being deleted or disconnected falls back to OpenRouter. The OpenAI and xAI adapters
were written from the providers' documentation and are tested against faked replies, not yet
against a live account. Whichever model is configured (`image_model` in Settings; nothing
generates if it's left blank or if `image_gen_enabled` is off).

## Reading pictures

Every picture read goes through `chat.vision()`: working-folder images, photos sent in chat,
phone and browser screenshots, and the avatar builder's upload. It sends the picture to the
connection and model chosen under **Settings → Images → reading pictures** (`vision_provider`,
`vision_model`, 2026-11-03; `chat.vision_backend`). Before this, reading pictures was
OpenRouter only. An empty OpenRouter balance stopped every picture read, even with a
connected ChatGPT sign-in whose model can see.

- **Any connection can be chosen.** Each adapter now carries a picture in its own format:
  - OpenRouter, Copilot, and OpenAI or xAI with an API key: the chat-completions `image_url`
    part. Copilot also gets its `copilot-vision-request` header.
  - The Responses endpoints (the ChatGPT and Grok sign-ins, Copilot's `/responses`): an
    `input_image` part.
  - Anthropic: an `image` block.
- **The model dropdown** lists what each provider says can see: OpenRouter's
  `input_modalities`, Copilot's `supports.vision`, and xAI's `/language-models`. All Claude
  models are listed. For OpenAI, its chat families are listed and its audio, speech, embedding
  and image models are left out. The models already set up for that connection under Model
  config come first. The ChatGPT sign-in has no model list, so only those appear there, or you
  type a name.
- **Defaults and fallback.** The default is OpenRouter with `NORI_VISION_MODEL`, as before.
  Choosing another connection picks the first model you use with it. A chosen connection that
  is deleted or disconnected falls back to OpenRouter.
- **When a read fails,** the provider's reason comes back (`workfiles.read_file` says the
  model couldn't read it, not that vision is off).

The settings that let a picture be sent at all (`workfile_vision_enabled`,
`chat_vision_enabled`) are unchanged.

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

## Her face

`generate_image_selfie` is anchored to `static/avatars/source/neutral.png`. The
[avatar builder](avatar-builder.md) replaces that file (and the avatars) when an
admin installs a new set, and can rewrite the style text above so selfies describe
the new character. Its spend is logged as purpose `avatar_builder` and left out of
today's household cap.

## Extending it

A different image provider or a second reference-image mechanism
(per-user, selectable, the way this module's counterpart in the
other, separate self-hosted app in this repository offers) is a real
design decision, not a drop-in swap: see [Contributing](contributing.md),
and note that Nori's own single fixed reference (she has exactly one
face) is a deliberate simplification, not a limitation to work around
quietly.

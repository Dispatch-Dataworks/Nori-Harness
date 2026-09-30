# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""OpenRouter chat-completions wrapper, plus run() -- the full turn
including any tool-calling rounds. Every request carries
{"provider": {"zdr": true}} -- zero-data-retention enforced on every call,
no exceptions, same as a sibling application and the editor before it.

No retry-status recording to a runtime/status table yet (that's part of
the Phase 10 settings UI, not needed to reach a working, tool-using chat).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import time
import urllib.error
import urllib.request

import config
import context
import conversation
import own_output
import precheck
import timing
import tools

API_URL = "https://openrouter.ai/api/v1/chat/completions"
# x-ai/grok-4.3: chosen for native tool-calling support -- switching this
# later needs no other code change. Operator overrides via NORI_MODEL in
# their own .env.
DEFAULT_MODEL = os.environ.get("NORI_MODEL", "x-ai/grok-4.3")
DEFAULT_MAX_TOKENS = int(os.environ.get("NORI_MAX_TOKENS", "1024"))
DEFAULT_TEMPERATURE = float(os.environ.get("NORI_TEMPERATURE", "0.8"))
MAX_TOOL_ROUNDS = int(os.environ.get("NORI_MAX_TOOL_ROUNDS", "4"))
# 300s, deliberately (2026-09-25, replacing the old 120 -- inherited, never chosen, and proven not to bind
# anyway on a sibling application's identical code: a real call there ran 1707s against it, app-wide-lock held the whole
# time). Long enough to cover a genuinely slow reasoning-heavy or multimodal (image-input) generation without
# tripping on normal variance; short enough that a real hang surfaces within minutes, not half an hour.
API_TIMEOUT_S = int(os.environ.get("NORI_API_TIMEOUT_S", "300"))
API_RETRIES = int(os.environ.get("NORI_API_RETRIES", "2"))

# A real wall-clock ceiling on the whole model call, not urlopen's own
# per-connect/per-read timeout= (see the incident note on API_TIMEOUT_S
# just above -- found on a sibling application's identical code, fixed in both trees
# the same way). Running the actual urlopen on a worker thread and
# imposing the deadline via Future.result(timeout=...) from the CALLING
# thread is what makes API_TIMEOUT_S mean total elapsed time: if the
# worker hasn't returned by the deadline, call() returns control regardless
# of what the abandoned worker thread does afterward -- never cancelled
# (Python threads can't be), just left to finish or fail on its own,
# harmlessly, since nothing waits on it once its deadline has passed.
_CALL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="model-call")


def _urlopen_json(req, socket_timeout: int) -> dict:
    with urllib.request.urlopen(req, timeout=socket_timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
# Same default a sibling application uses -- confirmed ZDR-eligible there, and vision
# support doesn't exist in Nori at all until workfiles.py's image reads.
VISION_MODEL = os.environ.get("NORI_VISION_MODEL", "qwen/qwen3-vl-235b-a22b-instruct")

# Models confirmed, with a real call this session, to accept image input
# directly -- ported from a sibling application's identical _MULTIMODAL_MODELS/
# model_accepts_images(). openai/gpt-5.6-luna (her real default_model_slug)
# confirmed 2026-09-15 with a real multimodal call: asked to name the
# dominant background color of a real test image, answered correctly
# ("Black") rather than erroring or guessing generically. Gates whether a
# casual chat photo goes to her as real multimodal content on the turn
# it's introduced (imagegen.chat_photo_extra_user_multimodal) or only
# ever as the cached text description (imagegen.chat_photo_extra_user,
# conversation.render_for_model) -- see model_accepts_images().
_MULTIMODAL_MODELS = {"openai/gpt-5.6-luna"}


def model_accepts_images(model: str) -> bool:
    """No fallback-to-DEFAULT_MODEL here on purpose, unlike a sibling application's
    identical function -- a sibling application is single-tenant, so its bare
    config.get("model") genuinely IS the one active model; Nori's is
    workspace-scoped and resolved via _resolve_model(workspace_id), so a
    caller must resolve and pass the REAL active slug explicitly rather
    than risk silently checking the wrong one against DEFAULT_MODEL,
    which is only a last-resort env-var fallback, not what's actually
    configured for any real workspace."""
    return model in _MULTIMODAL_MODELS


class ModelError(Exception):
    def __init__(self, msg, *, transient=False, kind=None):
        super().__init__(msg)
        self.transient = transient
        # kind is None for everything this class already raised before
        # 2026-09-25 -- only the wall-clock ceiling in call() (below) sets
        # one, "blocked_upstream": the provider never answered at all
        # within API_TIMEOUT_S, distinct from every other failure here,
        # which all mean it DID answer (with an error, or nothing useful).
        # A sibling application's own ModelError has the same field, plus a fuller
        # kind taxonomy this app hasn't needed yet -- not duplicated here
        # until nori actually needs more than this one distinction.
        self.kind = kind
        # Logged here, at construction -- the one point every raise site
        # (blank key, a bad model slug, the provider itself erroring, the
        # wall-clock ceiling, ...) shares, and the one point no caller can
        # accidentally swallow (2026-09-26, the operator: "a configuration failure
        # that produces nothing server-side is a defect, not a behaviour
        # to describe... an operator without UI access to someone else's
        # instance has only the logs"). Whatever a given catch site goes
        # on to do with this -- show it in the UI, retry, ignore -- the
        # fact that it happened has already reached docker compose logs.
        print(f"chat.ModelError: {msg}", flush=True)


def _key() -> str:
    k = os.environ.get("OPENROUTER_API_KEY", "")
    if not k:
        raise ModelError("OPENROUTER_API_KEY not set — each operator brings their own key (see .env.example)")
    return k


def _check(data: dict) -> None:
    if data.get("error"):
        raise ModelError(f"api error: {str(data['error'])[:300]}", transient=True)
    ch = (data.get("choices") or [{}])[0]
    if ch.get("error") or ch.get("finish_reason") == "error":
        raise ModelError(f"provider failed mid-response: {str(ch.get('error'))[:300]}", transient=True)


# Direct-provider access (2026-09-12, operator's own instruction: "if a
# direct key exists for a provider, prefer it" -- generalized, not an
# OpenAI-only special case even though OpenAI is the only one with a
# direct key today). Model slugs already carry the vendor as their own
# OpenRouter-style prefix (x-ai/grok-4.3, openai/gpt-4.1-mini, ...) --
# reusing that as the provider id costs nothing (no schema change to
# models.py, no second place a model's vendor could get out of sync with
# its slug) and degrades safely for a slug that doesn't look like
# vendor/name at all (split on "/" once; a bare string with no "/" just
# never matches a key below and takes the OpenRouter path like always).
# Adding a second direct provider later (Anthropic, Google, ...) is one
# more entry here, not new branching logic.
_DIRECT_PROVIDERS = {
    "openai": {"env": "OAI_API_KEY", "url": "https://api.openai.com/v1/chat/completions"},
}

# Per-model wire quirk (2026-09-14, the models page's confusing-pair fix --
# see models.py's own _VALID_EFFORTS comment for the full story): an admin
# only ever picks "no reasoning" once, the same way for every model --
# reasoning_effort resolves to None/"" either way. Most direct-provider
# models are fine being sent nothing at all in that case (the pre-existing
# behavior, unchanged). gpt-5.6-luna's own native API is the one confirmed
# exception: it 400s on OMITTING reasoning_effort outright whenever tools
# are present, and only the literal string "none" satisfies it. Keyed by
# native_model (the part after "vendor/") since that's what _call_direct
# already has in hand -- add a model here only once its own omission
# behavior has actually been confirmed broken the same way, not
# preemptively for every new addition to the roster.
_REQUIRES_LITERAL_NONE = {"gpt-5.6-luna"}


def _direct_key(provider: str) -> str | None:
    spec = _DIRECT_PROVIDERS.get(provider)
    if spec is None:
        return None
    return os.environ.get(spec["env"]) or None


def _call_direct(messages: list[dict], *, provider: str, native_model: str, max_tokens: int,
                 temperature: float, timeout: int, tools: list | None, tool_choice,
                 want_json: bool, reasoning_effort: str | None, api_key: str | None = None) -> dict:
    """Same request the OpenRouter path below would have made, translated
    to that provider's own native Chat Completions shape and sent straight
    to them, bypassing OpenRouter entirely. Returns the identical
    {"content", "finish_reason", "tool_calls", "usage"} shape call() always
    returns, so run() never has to know or care which path served a given
    turn.

    Two things OpenRouter's shape has that a native call doesn't, and what
    stands in for each:
      - `provider: {zdr: true}` -- a routing hint for OpenRouter to pick
        among ITS upstream providers; meaningless once we're not going
        through OpenRouter's aggregation at all, so there's nothing to set
        here, not a dropped protection.
      - `usage.cost` -- OpenRouter computes and returns a real dollar
        figure; a provider's own native API doesn't. usage["cost"] is set
        to None here, explicitly, rather than omitted or guessed at a
        stale published rate -- an honest gap a caller can check for
        (`usage.get("cost") is not None`), not a silent zero.

    reasoning_effort is sent as a flat top-level field (this provider's own
    native convention) rather than OpenRouter's nested {"reasoning":
    {"effort": ...}} wrapper -- if a given model on this provider doesn't
    actually accept that field, the request 400s, this raises ModelError,
    and call()'s caller falls back to OpenRouter for this same turn rather
    than failing it outright. That fallback isn't just a safety net for a
    down provider -- it's also what absorbs a per-model shape mismatch
    like this one without needing to special-case every model in advance.
    """
    spec = _DIRECT_PROVIDERS[provider]
    # max_completion_tokens, not max_tokens -- found live (2026-09-13),
    # not assumed: gpt-5.6-luna's real API 400s on max_tokens with
    # "Unsupported parameter... use max_completion_tokens instead" (a
    # newer-model convention OpenRouter's own unified shape abstracts
    # away, which is exactly the kind of provider-native detail going
    # direct exposes). The fallback above already would have absorbed
    # this as a per-model shape mismatch either way -- fixed properly
    # instead of just leaning on the safety net for Nori's own actual
    # default model.
    body = {"model": native_model, "messages": messages, "max_completion_tokens": max_tokens}
    # temperature omitted entirely for a reasoning-effort call -- found
    # live (2026-09-13), same story as max_completion_tokens just above:
    # gpt-5.6-luna's real API 400s on any non-default temperature
    # ("Only the default (1) value is supported"), a real reasoning-model
    # constraint OpenRouter's own unified shape also abstracts away.
    # reasoning_effort being set is already this project's own existing
    # signal for "this is a reasoning model" (see the OpenRouter path
    # just below, which only sends its own reasoning wrapper under the
    # same condition) -- reused here rather than inventing a second one.
    if not reasoning_effort:
        body["temperature"] = temperature
    if want_json:
        body["response_format"] = {"type": "json_object"}
    if tools:
        body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    elif native_model in _REQUIRES_LITERAL_NONE:
        # "No reasoning" was chosen, but this model's own API rejects
        # omission outright when tools are present -- send the one wire
        # value it actually accepts instead. See _REQUIRES_LITERAL_NONE's
        # own comment above; the admin never had to know this.
        body["reasoning_effort"] = "none"
    # api_key override (2026-09-30, see _call_openai_api_key) -- an
    # explicitly configured Provider's own key takes precedence over the
    # legacy env-var-only OAI_API_KEY lookup, same "caller already
    # resolved this" precedence call() itself makes for its own api_key
    # override.
    key = api_key or _direct_key(provider)
    if not key:
        raise ModelError(f"no direct key configured for provider {provider!r}")
    last = None
    for attempt in range(API_RETRIES + 1):
        try:
            req = urllib.request.Request(
                spec["url"], data=json.dumps(body).encode("utf-8"), method="POST",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            _check(data)
            ch0 = (data.get("choices") or [{}])[0]
            msg = ch0.get("message") or {}
            usage = data.get("usage") or {}
            usage["cost"] = None  # see docstring -- this provider doesn't report one
            return {"content": msg.get("content") or "",
                    "finish_reason": ch0.get("finish_reason"),
                    "tool_calls": msg.get("tool_calls") or [],
                    "usage": usage}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            last = ModelError(f"direct {provider} call failed ({exc.code}): {detail}",
                              transient=exc.code >= 500)
        except (ModelError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
        if not getattr(last, "transient", True) or attempt == API_RETRIES:
            break
        time.sleep(1.5 * (attempt + 1))
    # attempt+1, not the hardcoded API_RETRIES+1 (2026-09-13, found while
    # reading real timing data) -- a non-transient failure (this model's
    # own 400 on tools+reasoning_effort together, chief example) breaks
    # out of the loop on its FIRST attempt, correctly not retrying
    # something that will never succeed -- but this message used to
    # always claim "after 3 attempt(s)" regardless, which reads as a
    # wasteful-retry bug on a quick single-shot rejection. The behavior
    # was always correct; only the wording was wrong.
    raise ModelError(f"direct {provider} call failed after {attempt + 1} attempt(s): {last}")


def call(messages: list[dict], *, model: str | None = None, max_tokens: int | None = None,
         temperature: float | None = None, timeout: int | None = None,
         tools: list | None = None, tool_choice: str | dict | None = None,
         want_json: bool = False, reasoning_effort: str | None = None,
         api_key: str | None = None, base_url: str | None = None,
         extra_headers: dict | None = None) -> dict:
    """api_key/base_url/extra_headers (2026-09-30, see providers.py) are
    overrides used by call_for_model()'s OpenRouter/Copilot adapters below
    -- both are OpenAI-chat-completions-shaped, so they reuse this
    function wholesale instead of duplicating its retry/timeout logic,
    just pointed at a different endpoint/key/headers. Every existing
    caller that doesn't pass these (vision(), the background-task callers
    that pass a bare model= slug) gets EXACTLY today's behavior: OpenRouter,
    _key()'s env-only key, no extra headers."""
    model_slug = model or DEFAULT_MODEL
    mtok = max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS
    temp = temperature if temperature is not None else DEFAULT_TEMPERATURE
    # A real bug, found 2026-09-13 investigating Luna's latency: once a
    # SPECIFIC model is named (model is not None), its own reasoning_effort
    # is authoritative -- None there means "this model's admin-chosen
    # setting is 'none -- do not send'" (models.py's own (none) option,
    # server.py's models admin form), not "nobody said, guess from the
    # environment." Only fall back to NORI_REASONING_EFFORT when model
    # ITSELF is also unresolved (None) -- _resolve_model()'s own
    # documented "None/None defers entirely to call()'s own env-var
    # defaults" case. Conflating these silently broke the models page's
    # "(none -- do not send)" option for every model, not just Luna: it
    # looked selectable and saved, but call() sent NORI_REASONING_EFFORT's
    # value anyway regardless, since a resolved model's own None and "no
    # model resolved at all" arrived here looking identical.
    effort = reasoning_effort if model is not None else (
        reasoning_effort if reasoning_effort is not None else os.environ.get("NORI_REASONING_EFFORT"))
    to = timeout if timeout is not None else API_TIMEOUT_S

    # Direct-provider access: prefer a direct key over OpenRouter whenever
    # one exists for this model's vendor (see _DIRECT_PROVIDERS/_call_direct
    # above). Falls through to the ordinary OpenRouter path below on ANY
    # failure -- missing key, network error, or the provider rejecting a
    # param this shape doesn't happen to support -- so this is strictly
    # additive: a model with no direct key configured, or a direct call
    # that fails for any reason, behaves exactly as it always has.
    # Legacy direct-provider vendor-sniffing (see _DIRECT_PROVIDERS above)
    # only applies when the caller hasn't already told us exactly where to
    # send this -- an api_key/base_url override (call_for_model()'s own
    # adapters) means a real Provider has already been resolved, and takes
    # precedence over guessing from the slug's vendor prefix.
    if api_key is None and base_url is None:
        provider = model_slug.split("/", 1)[0] if "/" in model_slug else ""
        if _direct_key(provider):
            native_model = model_slug.split("/", 1)[1]
            try:
                return _call_direct(messages, provider=provider, native_model=native_model,
                                    max_tokens=mtok, temperature=temp, timeout=to, tools=tools,
                                    tool_choice=tool_choice, want_json=want_json, reasoning_effort=effort)
            except ModelError as exc:
                print(f"chat.call: direct {provider} call for {model_slug!r} failed ({exc}) -- "
                     f"falling back to OpenRouter for this turn", flush=True)

    body = {
        "model": model_slug,
        "messages": messages,
        "max_tokens": mtok,
        "temperature": temp,
        # Real per-call dollar cost back in the response (usage.cost) --
        # not included by default; OpenRouter only computes/returns it
        # when explicitly asked. Needed to measure actual spend rather
        # than estimate it from a headline per-token rate, which reasoning
        # tokens (billed as output) can make meaningfully wrong.
        "usage": {"include": True},
    }
    if base_url is None:
        # OpenRouter-specific routing hint -- meaningless (and not sent)
        # once base_url points somewhere else, e.g. Copilot's own endpoint.
        body["provider"] = {"zdr": True}
    if want_json:
        body["response_format"] = {"type": "json_object"}
    if tools:
        body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
    # OpenRouter's unified reasoning-effort control (nested form -- the
    # documented canonical shape, over the reasoning_effort shorthand).
    # No-op (silently ignored by OpenRouter) for a model that doesn't
    # support reasoning at all, so this is safe to leave un-set for every
    # existing caller/model and only pass explicitly where it matters.
    if effort:
        body["reasoning"] = {"effort": effort}
    url = base_url or API_URL
    headers = {"Authorization": f"Bearer {api_key or _key()}", "Content-Type": "application/json",
              "X-Title": "nori"}
    if extra_headers:
        headers.update(extra_headers)
    last = None
    for attempt in range(API_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
            try:
                data = _CALL_EXECUTOR.submit(_urlopen_json, req, to).result(timeout=to)
            except concurrent.futures.TimeoutError:
                raise ModelError(
                    f"model call exceeded the {to}s ceiling with no response from the provider",
                    kind="blocked_upstream", transient=False)
            _check(data)
            ch0 = (data.get("choices") or [{}])[0]
            msg = ch0.get("message") or {}
            content = msg.get("content") or ""
            if not content.strip():
                # Same misrouted-into-reasoning fallback a sibling application needed for
                # Nebius/Hermes -- harmless no-op for any model that doesn't
                # do this, only fires when content is genuinely empty.
                content = (msg.get("reasoning") or "").strip()
            usage = data.get("usage") or {}
            # OpenRouter's own total (its price, markup included) lives at
            # usage.cost; usage.cost_details.upstream_inference_cost is the
            # raw underlying provider's cost, present alongside it -- normalize
            # to a single top-level usage["cost"] so a caller checking
            # usage.get("cost") gets a real number from either shape rather
            # than needing to know which field this particular response used.
            # Direct-provider calls (_call_direct, above) set this to None
            # explicitly for the same reason -- one field, checked the same
            # way, honest about not always having a value.
            if usage.get("cost") is None:
                upstream = (usage.get("cost_details") or {}).get("upstream_inference_cost")
                if upstream is not None:
                    usage["cost"] = upstream
            return {"content": content,
                    "finish_reason": ch0.get("finish_reason"),
                    "tool_calls": msg.get("tool_calls") or [],
                    "usage": usage}
        except (ModelError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            if not getattr(exc, "transient", True) or attempt == API_RETRIES:
                break
            time.sleep(1.5 * (attempt + 1))
    # Same fix as _call_direct's identical bug just above -- attempt+1,
    # not the hardcoded API_RETRIES+1: a non-transient failure breaks on
    # its first try, and the old message overstated the attempt count
    # regardless of how many actually happened.
    raise ModelError(f"model call failed after {attempt + 1} attempt(s): {last}", kind=getattr(last, "kind", None))


def vision(instruction: str, images: list[tuple[bytes, str]], *, model: str | None = None,
          max_tokens: int | None = None) -> str:
    """A single reading of one or more images by a vision-capable model --
    retyped from a sibling application's chat.vision(), same shape. tools=None on this
    call, same structural principle ingest.py's read pass uses: nothing
    about describing a picture should ever be able to invoke a tool.

    Returns plain prose. The CALLER is responsible for treating that prose
    as untrusted content before it reaches anywhere tool-capable -- an
    image can contain rendered text that's just as capable of smuggling an
    instruction as an email body is. workfiles.py is the one caller today,
    and it does route this through ingest.summarize_untrusted() before the
    caption reaches the model."""
    import base64
    content = [{"type": "text", "text": instruction}]
    for data, mime in images:
        b64 = base64.b64encode(data).decode("ascii")
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{mime or 'image/jpeg'};base64,{b64}"}})
    result = call([{"role": "user", "content": content}], tools=None,
                 model=model or VISION_MODEL, max_tokens=max_tokens or 400, temperature=0.0)
    return result["content"].strip()


# 120s, not a guess (2026-09-17, real timeout complaints from the operator) --
# raised from the old hardcoded 60 after measuring real generate_image_
# selfie/imagine_image durations in .nori.log's own TIMING lines: of 22
# real calls, 15 genuine successes ranged 34.6-54.2s, and 6 (27%) were
# ALREADY hitting the old 60s ceiling and failing -- not rare. The
# successes' own max (54.2s) plus a sibling application's own measured precedent for
# a comparable reference-anchored image model (seedream-5-0-lite, real
# generations up to ~95s with a reference image attached -- see
# a sibling application-app.md memory) both point well past 60s; 120s gives real
# margin over both rather than just nudging the same cliff a little
# further out. Confirmed this is a genuine provider-latency problem, NOT
# the 2026-09-13 IPv6/DNS-latency incident recurring (see server.py's own
# module-level socket.getaddrinfo patch) -- imagegen.py makes no network
# call of its own; it goes through THIS function, which uses
# urllib.request the same as every other outbound call in this process,
# so it already inherits that process-wide fix. Env-overridable, same
# posture as API_TIMEOUT_S/EMBED_TIMEOUT_S above.
IMAGE_TIMEOUT_S = int(os.environ.get("NORI_IMAGE_TIMEOUT_S", "120"))


def openrouter_image(prompt: str, *, model: str, seed: int | None = None, n: int = 1,
                     references: list[tuple[bytes, str]] | None = None) -> dict:
    """OpenRouter native Image API: POST /api/v1/images -> base64 back, real
    cost in usage.cost. Retyped from a sibling application's identical function, same
    shape (same discipline as everywhere else between these two apps --
    not imported). Same OPENROUTER_API_KEY, ZDR enforced. `references` is
    [(bytes, mime), ...] passed as input_references for character
    consistency -- this is what imagegen.py's reference-anchored
    generation actually relies on. Returns {ok, bytes, mime, cost} or
    {ok: False, reason}."""
    import base64
    body: dict = {"model": model, "prompt": prompt, "n": max(1, n), "provider": {"zdr": True}}
    if seed is not None and seed >= 0:
        body["seed"] = seed
    if references:
        body["input_references"] = [
            {"type": "image_url", "image_url": {
                "url": f"data:{mime or 'image/jpeg'};base64,{base64.b64encode(data).decode('ascii')}"}}
            for data, mime in references]
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/images", data=json.dumps(body).encode("utf-8"),
            method="POST", headers={"Authorization": f"Bearer {_key()}",
                                    "Content-Type": "application/json", "X-Title": "nori"})
        with urllib.request.urlopen(req, timeout=IMAGE_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "reason": f"image API {exc.code}: {detail}"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        # "timed out" (2026-09-17) is a distinguishable marker, not just a
        # message flourish -- imagegen.clean_image_error() matches on it to
        # tell her (and through her, the operator) plainly that this was a
        # timeout, not a content refusal or a generic failure. A slow wait
        # that ends in an ambiguous error is exactly the "worse than a fast
        # error" outcome the operator flagged -- the reason string itself
        # has to carry which failure this actually was.
        if "timed out" in str(exc).lower():
            return {"ok": False, "reason": f"image API call timed out after {IMAGE_TIMEOUT_S}s"}
        return {"ok": False, "reason": f"image API call failed: {exc}"}
    if data.get("error"):
        return {"ok": False, "reason": f"image API error: {str(data['error'])[:300]}"}
    items = data.get("data") or []
    if not items or not items[0].get("b64_json"):
        return {"ok": False, "reason": "image API returned no image"}
    try:
        raw = base64.b64decode(items[0]["b64_json"])
    except base64.binascii.Error as exc:  # type: ignore[attr-defined]
        return {"ok": False, "reason": f"bad base64 from image API: {exc}"}
    cost = (data.get("usage") or {}).get("cost")
    return {"ok": True, "bytes": raw, "mime": items[0].get("media_type") or "image/png",
            "cost": float(cost) if isinstance(cost, (int, float)) else None}


EMBED_API_URL = "https://openrouter.ai/api/v1/embeddings"
EMBED_TIMEOUT_S = int(os.environ.get("NORI_EMBED_TIMEOUT_S", "15"))


def openrouter_embed(texts: list[str], *, model: str) -> dict:
    """OpenRouter's embeddings endpoint (added ~August 2026) -- chosen
    specifically because it reuses OPENROUTER_API_KEY, the one credential
    this app already requires, rather than adding a second vendor/key just
    for memory.py's topic-activation feature. Same ok/reason shape as
    openrouter_image() above. Short timeout (default 15s, well under
    chat.py's own 120s model-call timeout) -- this is called from inside a
    live tool-calling round (memory.preaction_check, point 6's own
    pre-action hook) as well as the ordinary per-turn precheck, and an
    embedding call hanging as long as a real chat completion would turn a
    cheap safety check into the slowest part of the turn. A timeout or any
    other failure comes back as {"ok": False, "reason": ...} -- callers
    (memory.py) are responsible for treating that as "couldn't check," not
    "checked, nothing there" -- an outage must never look like an all-clear.

    Returns {"ok": True, "vectors": [[float, ...], ...]} in the same order
    as `texts`, or {"ok": False, "reason": str}."""
    body = {"model": model, "input": texts}
    try:
        req = urllib.request.Request(
            EMBED_API_URL, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json",
                    "X-Title": "nori"})
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "reason": f"embeddings API {exc.code}: {detail}"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ModelError) as exc:
        return {"ok": False, "reason": f"embeddings API call failed: {exc}"}
    if data.get("error"):
        return {"ok": False, "reason": f"embeddings API error: {str(data['error'])[:300]}"}
    items = data.get("data") or []
    if len(items) != len(texts):
        return {"ok": False, "reason": f"embeddings API returned {len(items)} vectors for {len(texts)} inputs"}
    try:
        vectors = [it["embedding"] for it in items]
    except (KeyError, TypeError) as exc:
        return {"ok": False, "reason": f"embeddings API returned an unexpected shape: {exc}"}
    return {"ok": True, "vectors": vectors}


# Leaked tool call detection (2026-09-12) -- a real production incident on
# a sibling application's side: a model with real, native tool calling wrote
# `[send_image prompt: ...]` directly into her reply instead of making a
# real call. tool_calls came back genuinely empty; nothing in this app's
# own code mangled anything. Guidance prompts are the wrong layer to rely
# on alone here -- they're probabilistic, and this same session watched one
# trimmed line silently regress this exact behavior. This is the
# deterministic backstop: checked only when a round's real tool_calls came
# back empty (zero cost on every healthy turn), matched structurally
# (syntax AROUND an actually-active tool name -- brackets/backticks/angle
# brackets/call-verbs/a raw {"name": ...} blob), never on the bare name
# alone, so a reply that legitimately discusses a tool by name (asked what
# she can do, a tools-list page) doesn't trip it. Tested against both
# sides empirically before wiring in.
_LEAK_NUDGE = {"role": "user", "content": (
    "Your last reply described using a tool in the text instead of actually calling it "
    "through the real tool-calling mechanism. Make the real tool call now -- don't write "
    "it out, don't mention this correction to him.")}

_LEAK_FAILURE_TEXT = "(something didn't go through — try asking again)"


# The pattern itself lives in own_output.py (one source, used at generation time here and at render time there).
_leak_pattern = own_output.leak_pattern


def _detect_leaked_call(text: str, leak_re: re.Pattern | None) -> str | None:
    """Returns the matched fragment (for logging), or None. Only meaningful
    to call when this round's real tool_calls came back empty -- a leak
    pattern found alongside a REAL call isn't checked (out of scope for
    this pass; the real call already succeeded)."""
    if not text or leak_re is None:
        return None
    m = leak_re.search(text)
    return m.group(0) if m else None


def _resolve_model_chain(workspace_id: int) -> list[dict]:
    """This workspace's primary+fallback models, in order, each with its
    provider nested (see models.get_chain()) -- empty if nothing's
    configured, or every configured entry's provider/model has since been
    disabled -- never a hard failure over a settings gap; call_via_chain()
    turns an empty chain into one clear ModelError instead."""
    import models  # local: models.py has no import chain back to chat.py, but kept local/lazy for consistency with the pattern elsewhere in this file
    return models.get_chain(workspace_id)


def _resolve_model(workspace_id: int) -> tuple[str | None, str | None]:
    """Back-compat convenience for callers that just want the PRIMARY
    model's (model_name, reasoning_effort) for display/capability checks
    (server.py's status page, model_accepts_images()) -- not for making a
    call, which should go through call_via_chain() instead. None/None
    means nothing's configured for this workspace."""
    chain = _resolve_model_chain(workspace_id)
    if not chain:
        return None, None
    return chain[0]["model_name"], chain[0]["reasoning_effort"]


# ── Provider dispatch (2026-09-30, see providers.py) ───────────────────
# OpenRouter and GitHub Copilot are both OpenAI-chat-completions-shaped,
# so their adapters just reuse call() above with a different base_url/
# api_key/headers. Anthropic and OpenAI's OAuth-subscription endpoints
# are NOT chat-completions-shaped (Anthropic's own Messages API; OpenAI's
# ChatGPT-backend Responses-style endpoint) and need real request/response
# translation -- see each adapter's own docstring for exactly how
# confident that translation is; both are unofficial, undocumented paths.

def _call_openrouter(entry: dict, messages: list[dict], *, tools=None, tool_choice=None,
                     want_json=False, max_tokens=None, temperature=None, timeout=None) -> dict:
    import providers
    key = providers.api_key_for(entry["provider"])
    return call(messages, model=entry["model_name"], reasoning_effort=entry["reasoning_effort"],
               tools=tools, tool_choice=tool_choice, want_json=want_json, max_tokens=max_tokens,
               temperature=temperature, timeout=timeout, api_key=key)


def _call_github_copilot(entry: dict, messages: list[dict], *, tools=None, tool_choice=None,
                         want_json=False, max_tokens=None, temperature=None, timeout=None) -> dict:
    import providers
    token = providers.access_token_for(entry["provider"])
    headers = {"copilot-integration-id": "vscode-chat", "openai-intent": "conversation-panel",
              "x-github-api-version": "2025-04-01", **providers._COPILOT_HEADERS}
    return call(messages, model=entry["model_name"], reasoning_effort=entry["reasoning_effort"],
               tools=tools, tool_choice=tool_choice, want_json=want_json, max_tokens=max_tokens,
               temperature=temperature, timeout=timeout, api_key=token,
               base_url=providers.COPILOT_COMPLETIONS_URL, extra_headers=headers)


_ANTHROPIC_CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."


def _messages_to_anthropic(messages: list[dict], *, claude_code_identity: bool = False) -> tuple[list[dict], list[dict]]:
    """Nori's messages are OpenAI-chat-completions-shaped (a "system" role
    message, "tool" role results, assistant tool_calls); Anthropic's
    Messages API takes system as a separate top-level param and wants
    tool results as content blocks inside a user message. Best-effort
    translation covering plain text turns and simple tool round-trips --
    NOT yet fully verified against a real streaming/tool-heavy
    conversation (see _call_anthropic_oauth's own docstring).

    claude_code_identity=True (OAuth only, see _call_anthropic_oauth)
    prepends the "You are Claude Code" system block Anthropic's Cloudflare
    gate requires alongside an OAuth bearer token. A real API key
    (_call_anthropic_api_key) is a normal, fully-supported call -- sending
    that fake identity there would be pointless mimicry with no gate to
    satisfy, so it's opt-in, not the default."""
    system_blocks = [{"type": "text", "text": _ANTHROPIC_CLAUDE_CODE_SYSTEM}] if claude_code_identity else []
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            system_blocks.append({"type": "text", "text": m.get("content") or ""})
        elif role == "tool":
            out.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": m.get("tool_call_id"),
                "content": m.get("content") or ""}]})
        elif role == "assistant" and m.get("tool_calls"):
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id"), "name": fn.get("name"), "input": args})
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return system_blocks, out


def _tools_to_anthropic(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    out = []
    for t in tools:
        fn = t.get("function") or {}
        out.append({"name": fn.get("name"), "description": fn.get("description") or "",
                   "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


def _anthropic_messages_request(model_name: str, messages: list[dict], tools: list[dict] | None,
                                reasoning_effort: str | None, max_tokens, temperature, timeout,
                                headers: dict, *, claude_code_identity: bool) -> dict:
    """Shared request-build + HTTP call + response parsing for both
    Anthropic adapters (OAuth and API-key, below) -- only auth headers and
    the identity system block differ between them; the wire shape and
    response translation are identical either way, so this is the one
    place that needs updating if either changes."""
    import providers
    system_blocks, anth_messages = _messages_to_anthropic(messages, claude_code_identity=claude_code_identity)
    body: dict = {"model": model_name, "messages": anth_messages, "system": system_blocks,
                 "max_tokens": max_tokens or DEFAULT_MAX_TOKENS}
    if temperature is not None:
        body["temperature"] = temperature
    anth_tools = _tools_to_anthropic(tools)
    if anth_tools:
        body["tools"] = anth_tools
    if reasoning_effort:
        body["thinking"] = {"type": "enabled", "budget_tokens": 4096}
    to = timeout or API_TIMEOUT_S
    req = urllib.request.Request(providers.ANTHROPIC_MESSAGES_URL, data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers=headers)
    try:
        data = _CALL_EXECUTOR.submit(_urlopen_json, req, to).result(timeout=to)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise ModelError(f"Anthropic call failed ({exc.code}): {detail}", transient=exc.code >= 500)
    except (urllib.error.URLError, TimeoutError, concurrent.futures.TimeoutError, json.JSONDecodeError) as exc:
        raise ModelError(f"Anthropic call failed: {exc}")
    if data.get("error"):
        raise ModelError(f"Anthropic api error: {str(data['error'])[:300]}", transient=True)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    tool_calls = [{"id": b.get("id"), "type": "function",
                  "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input") or {})}}
                 for b in data.get("content", []) if b.get("type") == "tool_use"]
    usage = data.get("usage") or {}
    return {"content": text, "finish_reason": data.get("stop_reason"), "tool_calls": tool_calls,
           "usage": {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens"),
                     "cost": None}}


def _call_anthropic_oauth(entry: dict, messages: list[dict], *, tools=None, tool_choice=None,
                          want_json=False, max_tokens=None, temperature=None, timeout=None) -> dict:
    """Anthropic gates OAuth-bearer-token requests on looking like Claude
    Code itself (the claude-code-20250219/oauth-2025-04-20 beta headers
    plus the identity system block _anthropic_messages_request adds) --
    confirmed against public reports at the time this was written. There
    is also a real, reported possibility that a Claude Code OAuth token is
    scoped to the Claude Code client specifically and simply refuses
    third-party Messages API calls no matter what headers accompany it --
    if every call through this adapter fails with a 401/403 despite a
    valid, freshly-refreshed token, that's the likely cause, not a bug
    here. See _call_anthropic_api_key for the (much simpler) real-API-key
    path, which needs none of this mimicry."""
    import providers
    token = providers.access_token_for(entry["provider"])
    headers = {"Authorization": f"Bearer {token}", "anthropic-version": "2023-06-01",
              "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
              "x-app": "cli", "User-Agent": "claude-cli/1.0.56 (external, cli)"}
    return _anthropic_messages_request(entry["model_name"], messages, tools, entry["reasoning_effort"],
                                       max_tokens, temperature, timeout, headers, claude_code_identity=True)


def _call_anthropic_api_key(entry: dict, messages: list[dict], *, tools=None, tool_choice=None,
                            want_json=False, max_tokens=None, temperature=None, timeout=None) -> dict:
    """A real Anthropic API key against the real Messages API -- no OAuth,
    no Claude-Code mimicry, none of _call_anthropic_oauth's gate-related
    caveats. Same request/response translation either way (see
    _anthropic_messages_request); only the auth header differs."""
    import providers
    key = providers.api_key_for(entry["provider"])
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return _anthropic_messages_request(entry["model_name"], messages, tools, entry["reasoning_effort"],
                                       max_tokens, temperature, timeout, headers, claude_code_identity=False)


def _call_openai_api_key(entry: dict, messages: list[dict], *, tools=None, tool_choice=None,
                         want_json=False, max_tokens=None, temperature=None, timeout=None) -> dict:
    """A real OpenAI API key against the real, public Chat Completions API
    -- reuses _call_direct (the same OpenAI-native request shape the
    legacy env-var-only direct-OpenAI path already gets right:
    max_completion_tokens not max_tokens, temperature dropped for a
    reasoning call, flat reasoning_effort not OpenRouter's nested wrapper)
    rather than call()'s plain OpenRouter-shaped body, which real OpenAI
    models -- especially reasoning ones -- 400 on. Only the key source
    differs from the legacy path: this Provider's own key, not
    OAI_API_KEY."""
    import providers
    key = providers.api_key_for(entry["provider"])
    mtok = max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS
    temp = temperature if temperature is not None else DEFAULT_TEMPERATURE
    to = timeout if timeout is not None else API_TIMEOUT_S
    return _call_direct(messages, provider="openai", native_model=entry["model_name"], max_tokens=mtok,
                        temperature=temp, timeout=to, tools=tools, tool_choice=tool_choice,
                        want_json=want_json, reasoning_effort=entry["reasoning_effort"], api_key=key)


def _call_openai_oauth(entry: dict, messages: list[dict], *, tools=None, tool_choice=None,
                       want_json=False, max_tokens=None, temperature=None, timeout=None) -> dict:
    """The ChatGPT-backend Codex endpoint (chatgpt.com/backend-api/codex/
    responses) is NOT the public OpenAI Chat Completions API -- it's the
    same Responses-style endpoint the Codex CLI itself talks to over its
    OAuth session, undocumented beyond what's visible in Codex's own
    issue tracker at the time this was written. This translation (a
    Responses-API-shaped {"input": [...]} body, text extracted back out
    of output[].content[].text) is a best-effort reading of that public
    information, refined against real errors from a real connected
    account as they surface (store:false, below, was the first) rather
    than fully verified up front -- still treat a new failure through
    this adapter as likely needing another real fix here, not a bug
    elsewhere.
    """
    import providers
    token = providers.access_token_for(entry["provider"])
    input_items = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            input_items.append({"type": "function_call_output", "call_id": m.get("tool_call_id"),
                               "output": m.get("content") or ""})
        elif role == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                input_items.append({"type": "function_call", "call_id": tc.get("id"),
                                   "name": fn.get("name"), "arguments": fn.get("arguments") or "{}"})
        else:
            input_items.append({"role": role, "content": m.get("content") or ""})
    # store:false is REQUIRED, not optional, on this endpoint -- found
    # live (2026-09-30): omitting it (the public Responses API's own
    # default, store:true) 400s with "Store must be set to false" on the
    # ChatGPT-backend path specifically, since it has nowhere to persist
    # a stored response the way the public API does.
    body: dict = {"model": entry["model_name"], "input": input_items, "stream": False, "store": False}
    if tools:
        body["tools"] = [{"type": "function", "name": (t.get("function") or {}).get("name"),
                         "description": (t.get("function") or {}).get("description") or "",
                         "parameters": (t.get("function") or {}).get("parameters") or {}}
                        for t in tools]
    if entry["reasoning_effort"]:
        body["reasoning"] = {"effort": entry["reasoning_effort"]}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
              "chatgpt-account-id": (entry["provider"].get("account_meta") or {}).get("account_id", "")}
    to = timeout or API_TIMEOUT_S
    req = urllib.request.Request(providers.OPENAI_CODEX_URL, data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers=headers)
    try:
        data = _CALL_EXECUTOR.submit(_urlopen_json, req, to).result(timeout=to)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise ModelError(f"OpenAI OAuth call failed ({exc.code}): {detail}", transient=exc.code >= 500)
    except (urllib.error.URLError, TimeoutError, concurrent.futures.TimeoutError, json.JSONDecodeError) as exc:
        raise ModelError(f"OpenAI OAuth call failed: {exc}")
    if data.get("error"):
        raise ModelError(f"OpenAI OAuth api error: {str(data['error'])[:300]}", transient=True)
    text_parts, tool_calls = [], []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    text_parts.append(c.get("text", ""))
        elif item.get("type") == "function_call":
            tool_calls.append({"id": item.get("call_id"), "type": "function",
                              "function": {"name": item.get("name"), "arguments": item.get("arguments") or "{}"}})
    usage = data.get("usage") or {}
    return {"content": "".join(text_parts), "finish_reason": data.get("status"), "tool_calls": tool_calls,
           "usage": {"prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens"),
                     "cost": None}}


_PROVIDER_CALLERS = {
    "openrouter": _call_openrouter,
    "github_copilot": _call_github_copilot,
    "anthropic_oauth": _call_anthropic_oauth,
    "anthropic_api_key": _call_anthropic_api_key,
    "openai_oauth": _call_openai_oauth,
    "openai_api_key": _call_openai_api_key,
}


def call_for_model(entry: dict, messages: list[dict], **kw) -> dict:
    """Dispatch one completion call through a single resolved model
    (models.get_with_provider()'s shape -- a model row with "provider"
    nested). Raises ModelError on any failure; a caller wanting a
    fallback chain should use call_via_chain() instead, which catches
    this and tries the next model. jobs.py (sub-agents, exactly one model
    each, no chain) calls this directly."""
    provider = entry.get("provider")
    if provider is None:
        raise ModelError(f"model {entry.get('alias')!r} has no provider linked")
    caller = _PROVIDER_CALLERS.get(provider["type"])
    if caller is None:
        raise ModelError(f"unknown provider type {provider['type']!r}")
    return caller(entry, messages, **kw)


def call_via_chain(messages: list[dict], workspace_id: int, **kw) -> dict:
    """The primary-then-fallback loop: try this workspace's primary model,
    then each fallback in order, on ModelError -- generalizes the old
    direct-provider-then-OpenRouter fallback into the real Providers/
    Models chain. This is what run() below uses for every real turn."""
    chain = _resolve_model_chain(workspace_id)
    if not chain:
        raise ModelError(
            "no model configured for this workspace -- add a provider and a model in "
            "Settings > Models", transient=False)
    attempts = []
    for entry in chain:
        try:
            return call_for_model(entry, messages, **kw)
        except ModelError as exc:
            attempts.append(f"{entry['alias']}: {exc}")
    raise ModelError("every configured model failed for this turn -- " + "; ".join(attempts))


def run(session: dict, user_id: int, display_name: str, *, extra_message: dict | None = None,
       max_rounds: int | None = None, peer_pending: str | None = None,
       timing_turn: timing.Turn | None = None) -> dict:
    """One full turn, including any tool-calling rounds -- the model calls
    a tool, tools.dispatch() enforces role/scope and runs it for real, the
    result goes back as a tool message, repeat until a plain text reply or
    max_rounds is hit. Every tool call in this loop passes through the
    same single enforcement point real conversations and anything else that
    ever calls a tool will use.

    max_rounds, if not given, falls back to MAX_TOOL_ROUNDS (the old,
    single hardcoded value) -- callers should pass their own, since a live
    chat turn (someone's watching, can just ask her to keep going) and an
    unattended one (a proactive ping, a peer nudge, force_checkin's
    background thread -- nobody there to notice a stuck loop) carry
    different cost/runaway risk and were found, in real production use, to
    need different defaults: config.py's tool_rounds_chat/
    tool_rounds_proactive are the per-user-tunable versions of this.

    extra_message, if given, is appended after the real conversation
    history before the first model call -- used by scheduler.py to prompt
    an unprompted turn without a real user message triggering it.

    A visible "used <tool>" line (kind='tool', name only -- never
    arguments or results) is recorded per real dispatch, gated by the
    show_tool_calls setting (default on), except set_emotion, which updates
    the avatar without adding chat clutter. Still not persisted: the tool's
    JSON result and the intermediate assistant/tool messages that carry it
    through THIS function's own local `messages` list -- those exist only
    for this turn's model round-trip, same as before.

    peer_pending's default stays None -- don't inject (2026-09-13,
    tightened same day after finding a bare bool default of True had been
    backwards) -- a caller nobody's written yet gets pending peer content
    withheld until it explicitly asks for it, never by omission. As of
    2026-09-19, this is a two-valued switch, not a bool, because a message
    can be "unread" along two independent axes (see peers.
    pending_delivery_messages()'s own docstring on why one flag used to be
    overloaded for both and that was wrong): pass `"peer"` from a
    peer-motivated turn spawned by peers._run_prompted_turn (force_checkin,
    forced_checkin_tick, a granted reply_requested, peer_check_tick), and
    `"user"` from every call site answering a real message he sent --
    server.py's live /send /retry, and every _sweep across scheduler.py/
    jobs.py/peers.py that answers a message which arrived mid-turn (his
    own 2026-09-19 answer to "are peer messages actually in context on an
    ordinary turn": inject unread peer content into every turn FROM HIM,
    not just a peer-motivated one -- but a message already shown to a
    PEER-motivated turn is NOT thereby read for the "user" dimension, and
    vice versa; each is tracked and stamped independently). Still None
    (never omitted-as-if-default) for a turn nobody asked for -- Nori's own
    household/meal proactive ping, a due schedule, a due reminder -- since
    "from him" doesn't describe those; the pending message isn't lost,
    just deferred to the next turn of whichever kind does want it, same as
    before. Getting a NEW caller's value wrong is still the fail-closed
    bug this section was written to prevent -- explicitly say which of the
    three cases (`None`, `"peer"`, `"user"`) a new call site is in, don't
    leave it to the default.

    Returns {"text": str, "usage": {"prompt_tokens", "completion_tokens",
    "cost"}} (2026-09-13, per-turn cost logging -- was a bare string
    before this; every real caller updated the same day). usage["cost"]
    is the real, summed dollar cost across every round this turn actually
    made, in USD -- or None if ANY round in the turn didn't report one
    (a direct-provider call, chat.py's own _call_direct -- see call()'s
    own docstring). Never partially summed and presented as if it were
    the whole turn's cost: a real number here is a real, complete total,
    not an estimate or a floor. Token counts are always real regardless
    of provider, so those still get recorded even when cost doesn't."""
    rounds = max_rounds if max_rounds is not None else MAX_TOOL_ROUNDS
    t = timing_turn if timing_turn is not None else timing.NULL_TURN
    with t.stage("context_build"):
        messages = context.build_messages(user_id, display_name, peer_pending=peer_pending)
    if extra_message:
        messages.append(extra_message)
    # Deliberately appended LAST, after the real history and any
    # extra_message -- proximate by construction, not folded into
    # context.build_messages()'s own (persona-carrying, always-present)
    # system message. See precheck.py's own docstring for why position,
    # not wording, is the mechanism this exists to use.
    with t.stage("precheck"):
        checklist = precheck.build_block(session, user_id)
    if checklist:
        messages.append(checklist)
    schemas = tools.active_schemas(session)
    leak_re = _leak_pattern({s["function"]["name"] for s in schemas}) if schemas else None
    model_slug, _reasoning_effort = _resolve_model(session["workspace_id"])
    t.model = model_slug or DEFAULT_MODEL
    workspace_id = session["workspace_id"]
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
    cost_unavailable = False

    def _add_usage(u: dict) -> None:
        nonlocal cost_unavailable
        usage["prompt_tokens"] += u.get("prompt_tokens", 0) or 0
        usage["completion_tokens"] += u.get("completion_tokens", 0) or 0
        c = u.get("cost")
        if c is None:
            cost_unavailable = True
        elif not cost_unavailable:
            usage["cost"] += c

    def _finish(text: str, *, hit_round_limit: bool = False) -> dict:
        # hit_round_limit (2026-09-19, real gap found investigating why
        # a sibling application's peer-motivated turns sometimes produce no reply): a
        # turn that exhausts `rounds` without ever returning plain text
        # produces THIS fallback string, discarded exactly like any other
        # reply on a peer-motivated turn (that output is never sent to
        # anyone unless the model itself called peer{id}_send) --
        # mechanistically indistinguishable, before this flag existed,
        # from a turn that ran fine and genuinely chose not to reply. See
        # peers._run_prompted_turn's own new logging for where this is
        # read and made durable.
        return {"text": text, "usage": {**usage, "cost": None if cost_unavailable else usage["cost"]},
               "hit_round_limit": hit_round_limit}

    for rnd in range(1, rounds + 1):
        with t.stage("model_call", round=rnd):
            result = call_via_chain(messages, workspace_id, tools=schemas or None)
        _add_usage(result.get("usage") or {})
        calls = result.get("tool_calls") or []
        if not calls:
            text = (result.get("content") or "").strip()
            leak = _detect_leaked_call(text, leak_re)
            if not leak:
                return _finish(text or "(no reply — the model returned nothing)")
            print(f"chat.run round {rnd}: LEAKED TOOL CALL detected -- matched={leak!r} -- retrying once",
                 flush=True)
            with t.stage("model_call", round=rnd, retry="leak"):
                retry = call_via_chain(messages + [_LEAK_NUDGE], workspace_id, tools=schemas or None)
            _add_usage(retry.get("usage") or {})
            retry_calls = retry.get("tool_calls") or []
            if retry_calls:
                print(f"chat.run round {rnd}: retry recovered with a real tool call "
                     f"({', '.join((c.get('function') or {}).get('name', '?') for c in retry_calls)})",
                     flush=True)
                result, calls = retry, retry_calls
            else:
                retry_text = (retry.get("content") or "").strip()
                retry_leak = _detect_leaked_call(retry_text, leak_re)
                if retry_leak:
                    print(f"chat.run round {rnd}: retry ALSO leaked -- matched={retry_leak!r} -- "
                         f"failing loudly, not delivering the text", flush=True)
                    return _finish(_LEAK_FAILURE_TEXT)
                print(f"chat.run round {rnd}: retry recovered with clean text", flush=True)
                return _finish(retry_text or "(no reply — the model returned nothing)")
        messages.append({"role": "assistant", "content": result["content"] or None, "tool_calls": calls})
        for tc in calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            out = tools.dispatch(name, args, session, timing_turn=t, round=rnd)
            # set_emotion used to be excluded from ever getting a row here at
            # all -- stronger than VISIBLE_TOOLS_FILTER's own display-only
            # suppression (conversation.py), and the mismatch meant an
            # emotion change left no trace anywhere, not even in the history
            # view or tool activity (2026-09-14, operator's own ask: bring it
            # in line with message_user/send_image -- logged like any other
            # tool call, filtered from the chat view only, not from the data).
            if name and config.get("user", user_id, "show_tool_calls"):
                # failed (2026-09-19, real gap found in the "ensure
                # silence is truly her choice" sweep): this used to log
                # "used {name}" unconditionally, ignoring `out` entirely
                # -- a refused peer{id}_send (cooldown, a cap, the rate
                # limiter) read IDENTICALLY to a successful one. The
                # VISIBLE content stays "used {name}" on purpose --
                # server.py's own toolRunLine() collapses several
                # consecutive tool-call lines into "used X, Y, and N
                # more" by string-splitting on "used ", so embedding
                # failure text into content the way a sibling application's
                # toolactivity.describe() does would corrupt that
                # collapsing (a sibling application has no equivalent feature to
                # protect). The outcome instead lives in `meta`,
                # structured, for anything that actually needs to tell
                # success from failure -- see peers.py's own settings-
                # page diagnostics query for the first real consumer.
                failed = isinstance(out, dict) and (bool(out.get("error")) or out.get("ok") is False)
                meta = {"tool_name": name}
                if failed:
                    meta["failed"] = True
                    meta["error"] = str(out.get("error") or out.get("reason") or "could not complete")[:300]
                conversation.add_message(user_id, "assistant", f"used {name}", kind="tool", meta=meta)
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "content": json.dumps(out, default=str)})
            # Point 6 of the topic-activation spec (2026-09-17, memory.py) --
            # a consequential tool's own result is immediately followed by
            # a fresh memory check keyed off the ACTION's own name/
            # arguments, not the message that started the turn (a call
            # decided several rounds in has no fresh incoming message to
            # re-extract topics from). tools.is_consequential() is the
            # deliberate, per-tool flag this gates on -- see that module.
            if name and tools.is_consequential(name):
                import memory  # local: avoids a needless top-level dependency direction, same as elsewhere here
                note = memory.preaction_check(session, name, args)
                if note:
                    messages.append({"role": "system", "content": note})
    # Visible, not silent -- but also actionable: this used to just say "no
    # final reply", which read like a bug report, not a turn she "gave" you.
    # Found in real use: every real hit of this was a legitimate multi-step
    # task (MCP search -> get -> update), not a runaway loop, so the fix is
    # telling the operator this is a tunable knob, not just that something
    # broke.
    return _finish(f"(hit the {rounds}-step tool-call limit for this turn without finishing -- "
                   f"raise \"tool call rounds\" in Settings if tasks like this need more steps)",
                  hit_round_limit=True)

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Image generation -- two tools, ported from a sibling application's imagegen.py
(retyped, not imported, same discipline as everywhere else between these
two apps) but reworked into two distinct tools rather than one (operator's
own explicit ask, 2026-09-14):

  generate_image_selfie  always anchored to Nori's own face (the same
                          reference image every avatar PNG was generated
                          from -- see reference_bytes() below). For "send
                          me a selfie" / "what does it look like where
                          you are" -- anything that should recognizably
                          be HER.
  imagine_image           general-purpose: any scene, with an OPTIONAL
                          reference image supplied as a path already in
                          her own working folder, for when the picture
                          isn't of her at all.

Both share ONE household image budget (config.image_daily_cap_usd) --
operator's own decision: two tools drawing from two separate budgets
would just mean the one that happens to get used first silently
determines what's left for the other, which is a worse rule than not
splitting them at all. See medialog.spend_status() for the live
(never a maintained running counter) accounting this enforces against.

Only Nori has this -- a sibling application's own imagegen.py stays exactly as it is.
Nori has no per-user image_reference_file_id the way a sibling application does
(nothing to pick: she has exactly one face, the same one every avatar
was generated from, so the reference is a fixed bundled asset rather
than an admin-configurable upload).

Prompt assembly (persists regardless of what she writes), same shape as
a sibling application's own:
    <image_style_prefix>  <her scene prompt>  <image_clothed_constraint>

Cost: per-image, and she's autonomous, so there's a daily USD cap, shared
by both tools. Over it the tool returns a refusal (not an error).

Content-filter compliance -- staying within the image provider's own
policy, not evading it -- ported verbatim from a sibling application: prompt_flags()
is a cheap pre-check against the provider's actual blocked categories,
run before spending on a call that would obviously be refused unless
image_content_precheck_enabled is turned off in Settings > Images;
clean_image_error() turns a genuine provider refusal into something she
can act on instead of raw HTTP/JSON. image_clothed_constraint is the
belt-and-braces layer appended after whatever she writes, surfaced to her
identically via image_content_rules() so a refusal isn't a mystery.

Chat suppression: the automatic "used generate_image_selfie"/"used
imagine_image" tool-call line is filtered from the chat VIEW only (see
conversation.VISIBLE_TOOLS_FILTER), same display-only precedent as
set_emotion/message_user -- the actual generated image message
(kind='image') is never suppressed, obviously, or there'd be nothing to
show for the call at all.
"""
from __future__ import annotations

import os
import random
import re
import time
import uuid
from pathlib import Path

import config
import conversation
import medialog
import store
import tools

GEN_DIR = store.DATA_DIR / "generated"
GEN_DIR.mkdir(parents=True, exist_ok=True)

# Thumbnails (2026-09-15, operator's own ask: /photos was slow because it
# served full-size originals as grid tiles). Nested INSIDE GEN_DIR, not a
# sibling -- checked directly, not assumed: the operator's own snapshot tooling
# walks a sibling application's data/ with a plain rglob("*") (recursive, picks up any
# new subdirectory for free) and nothing in either app enumerates GEN_DIR
# expecting every entry to be a flat original file (confirmed: no code
# iterates GEN_DIR's own contents at all -- every reader goes straight to
# GEN_DIR / <known file_id>). Nesting here means the one place that already
# archives "the generated images directory" wholesale keeps doing exactly
# that, with zero changes to it, rather than needing to learn about a new
# sibling directory. THUMB_DIR is a derived cache, same category as a
# compiled .pyc -- safe to delete entirely and it rebuilds on next access,
# per-file, automatically (serve_thumbnail's own on-demand fallback).
THUMB_DIR = GEN_DIR / "thumbs"
THUMB_DIR.mkdir(parents=True, exist_ok=True)
THUMB_MAX_DIM = 400  # long edge, px -- covers a ~200px grid tile at 2x DPI with real margin
THUMB_JPEG_QUALITY = 78

_REFERENCE_PATH = Path(__file__).resolve().parent / "static" / "avatars" / "source" / "neutral.png"


_MIME_SIGS = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG\r\n\x1a\n": "image/png",
             b"GIF87a": "image/gif", b"GIF89a": "image/gif", b"RIFF": "image/webp"}


def sniff_mime(head: bytes) -> str:
    """Lenient -- always returns SOME image mime, defaulting to png, same
    shape as a sibling application's own imagegen._sniff (server.py's image_get calls
    this the same way a sibling application's serve_image calls that -- by the time
    a file is being re-served from GEN_DIR it already passed real
    validation once at generation time, so this is just picking the right
    Content-Type, not re-validating)."""
    for sig, m in _MIME_SIGS.items():
        if head.startswith(sig):
            if sig == b"RIFF":
                return "image/webp" if head[8:12] == b"WEBP" else "image/png"
            return m
    return "image/png"


def thumb_path(file_id: str) -> Path:
    return THUMB_DIR / f"{file_id}.jpg"


def generate_thumbnail(file_id: str) -> bool:
    """Generate-once, reuse forever -- returns True the moment a thumbnail
    exists on disk for this file_id, whether it already did or was just
    made now; False only if the real original is missing/unreadable, or
    Pillow itself isn't installed. Callers (serve_thumbnail, the backfill
    script) never need to know which of those happened -- False always
    means "fall back to the original," same handling either way.

    Pillow, not stdlib (2026-09-15, operator's own question: "what's
    available... say what you'd use rather than adding a heavyweight
    dependency silently"). Checked directly rather than assumed: the
    stdlib has no JPEG decoder at all and no general raster resize either
    way, so a real thumbnail of an arbitrary uploaded photo needs a real
    image library, full stop -- there's no stdlib-only version of this
    feature to build instead. Pillow is already the one this app's own
    tooling reaches for (see static/pwa/README.md's icon-regeneration
    script) and was confirmed actually installed for the exact
    interpreter this app runs on before writing a single line here, not
    assumed from it merely being importable in some other environment.
    It is NOT declared anywhere as a hard dependency, though -- INSTALL.md
    explicitly promises "stdlib only, no pip install needed" for this app,
    a real, deliberate constraint predating this feature, so the import is
    local and caught, and a missing Pillow degrades to "no thumbnails,
    grid serves originals" rather than an app that won't start. See
    INSTALL.md's own updated line for the one exception this creates.

    Written to a .tmp file and moved into place with os.replace() (atomic
    on both POSIX and Windows, unlike a plain write) -- this runs from
    request-handling threads (serve_thumbnail's on-demand path) as well as
    the offline backfill script, so two concurrent requests for the same
    never-yet-thumbnailed photo must never let one see the other's
    half-written file."""
    return generate_thumb_for(GEN_DIR / file_id, thumb_path(file_id), THUMB_MAX_DIM, THUMB_JPEG_QUALITY)


def generate_thumb_for(src: Path, dest: Path, max_dim: int, quality: int = THUMB_JPEG_QUALITY) -> bool:
    """The actual resize primitive generate_thumbnail() above calls,
    generalized over any source/destination path rather than GEN_DIR's own
    file_id convention (2026-09-16, operator's own ask: avatar images
    needed the identical generate-once-cache-forever resize, and the
    instruction was explicit -- reuse this, don't write a third resize
    path). Same generate-once check, same atomic .tmp-then-replace write,
    same graceful-fallback-on-failure -- server.py's avatar route treats a
    False return exactly like serve_thumbnail already does: fall back to
    the real original, never a broken image or a 500."""
    if dest.is_file():
        return True
    if not src.is_file():
        return False
    try:
        from PIL import Image
    except ImportError:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)  # belt-and-braces; see a sibling application's identical line
    tmp = dest.with_suffix(".tmp")
    try:
        with Image.open(src) as img:
            img = img.convert("RGB")
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            img.save(tmp, "JPEG", quality=quality, optimize=True)
        os.replace(tmp, dest)
        return True
    except Exception:  # noqa: BLE001 -- a corrupt/unsupported source image
                       # must fall back to serving the original, not 500
        tmp.unlink(missing_ok=True)
        return False


def reference_bytes() -> tuple[bytes, str] | None:
    """Nori's own face -- the same neutral.png every emotion-state avatar
    in static/avatars/ was generated from (confirmed by direct inspection,
    2026-09-14), so anchoring generate_image_selfie to it is what actually
    makes a generated selfie look recognizably like the avatars already
    in the app, not a second, independently-drifting face. A bundled
    asset, not a config value -- nothing for an admin to pick, since she
    has exactly one face."""
    if not _REFERENCE_PATH.is_file():
        return None
    return _REFERENCE_PATH.read_bytes(), "image/png"


# ── casual chat photos (2026-09-15, ported from a sibling application's identical
# mechanism -- she never had this before; every image message until now
# was one she generated herself) ─────────────────────────────────────────
_STRICT_SIGS = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG\r\n\x1a\n": "image/png",
               b"RIFF": "image/webp"}


def _sniff_strict(head: bytes) -> str | None:
    """Same signatures sniff_mime() above uses, but None when nothing
    matched -- that function is lenient (fine for re-serving a file
    that's already stored here; wrong for validating a fresh upload,
    since it can't say "unknown"). This is the version an upload gate
    actually needs, same reasoning as a sibling application's identical split
    between _sniff/_sniff_strict."""
    for sig, m in _STRICT_SIGS.items():
        if head.startswith(sig):
            if sig == b"RIFF":
                return "image/webp" if head[8:12] == b"WEBP" else None
            return m
    return None


def store_chat_upload(data: bytes) -> dict:
    """A casual photo sent directly in chat -- validated against the
    REAL bytes (not the filename or claimed content-type), written under
    a random id into the same GEN_DIR every generated selfie already
    lives in and is served from (/image/<id>) -- distinct from the
    working folder (workfiles.py), which is for files she's asked to
    read or write as part of a task, not something dropped into chat."""
    mime = _sniff_strict(data[:16])
    if mime not in ("image/jpeg", "image/png", "image/webp"):
        return {"ok": False, "reason": "that doesn't look like a JPEG, PNG or WebP image "
                                       "(the file's contents, not just its name, are checked)."}
    fid = uuid.uuid4().hex
    (GEN_DIR / fid).write_bytes(data)
    return {"ok": True, "file_id": fid, "mime": mime}


def describe_chat_photo(user_id: int, data: bytes, mime: str) -> dict | None:
    """Runs the vision model ONCE for an inbound casual chat photo,
    gated on chat_vision_enabled (config.py, distinct from
    workfile_vision_enabled -- see that key's own comment for why).
    Returns {"description", "model", "ts"} or None if the setting is
    off or the call fails. Callers store all three in the message's
    meta so conversation.render_for_model() can reuse the text every
    time this message is rendered -- in the window, or later folded
    into a compacted summary -- without a second vision call."""
    if not config.get("user", user_id, "chat_vision_enabled"):
        return None
    import chat
    try:
        text = chat.vision(
            "Describe what's in this photo in a few plain, factual sentences -- objects, any "
            "visible text, general composition. Not creative, not speculative.",
            [(data, mime)])
    except chat.ModelError as exc:
        print(f"chat-photo vision description failed: {exc}", flush=True)
        return None
    return {"description": text, "model": chat.VISION_MODEL, "ts": time.time()}


def chat_photo_extra_user(description: str | None, caption: str) -> dict:
    """The one-time framing for the turn a photo was JUST sent in --
    distinct from the persisted description, which future turns pick up
    via conversation.render_for_model() instead of this. description is
    whatever describe_chat_photo() returned; None covers both "vision is
    off" and "the call failed" -- either way she's told plainly she
    can't see it, matching the honest framing a sibling application already uses for
    the same gap, rather than staying silent about why. Returns a real
    chat.run()-ready message dict (see its own extra_message param), not
    a bare string."""
    cap_bit = f', captioned "{caption}"' if caption else ""
    if description is None:
        text = (f"He just sent a photo in chat{cap_bit}, but you can't see it right now -- "
                "respond to the fact he sent it; take his word for what's in it, or ask.")
    else:
        text = (f"He just sent a photo in chat{cap_bit}. What it shows: {description}\n\n"
               "Respond to it in character, as though you'd actually seen it -- this wasn't a "
               "tool call, you're just looking at what he sent.")
    return {"role": "user", "content": text}


def chat_photo_extra_user_multimodal(data: bytes, mime: str, caption: str) -> dict:
    """The direct-multimodal counterpart to chat_photo_extra_user -- used
    ONLY on the turn a photo is introduced, and only when
    chat.model_accepts_images() is true for her real active model. Real
    image, not a description of one. Every LATER render of this message
    still goes through the cached text description via
    conversation.render_for_model(), same as before -- this only
    changes what happens on the one turn the photo actually arrives in."""
    import base64
    b64 = base64.b64encode(data).decode("ascii")
    cap_bit = f', captioned "{caption}"' if caption else ""
    text = (f"He just sent this photo in chat{cap_bit}. Respond to it in character, as though "
           "you'd actually seen it -- because you just did.")
    return {"role": "user", "content": [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": f"data:{mime or 'image/jpeg'};base64,{b64}"}},
    ]}


# Same reasoning as a sibling application's own _ANTI_COLLAGE (2026-09-10 there): the
# reference is a single portrait here, not a multi-panel character sheet,
# but the guard costs nothing and protects against the same failure mode
# (seedream-5-0-lite reproducing a grid/contact-sheet layout instead of one
# coherent scene) on any reference-anchored call.
_ANTI_COLLAGE = (" A single ordinary photograph of one person -- not a collage, grid, "
                 "contact sheet, comparison chart or split image.")


def assemble_prompt(scene: str, workspace_id: int, *, guard_collage: bool = False) -> str:
    prefix = config.get("workspace", workspace_id, "image_style_prefix").strip()
    constraint = config.get("workspace", workspace_id, "image_clothed_constraint").strip()
    parts = [prefix, scene.strip(), constraint]
    out = " ".join(p for p in parts if p)
    if guard_collage:
        out += _ANTI_COLLAGE
    return out


def image_content_rules(workspace_id: int) -> str:
    """Same text used in assemble_prompt's always-appended suffix and
    surfaced to her directly (tools.capabilities_block-equivalent) so a
    refusal isn't a mystery -- one config value, two callers."""
    return config.get("workspace", workspace_id, "image_clothed_constraint").strip()


# ── content-filter pre-check -- ported verbatim from a sibling application/imagegen.py
# (2026-09-10 there; see its own module docstring for the full reasoning:
# conservative, word-boundary, only the plain obvious cases, anything
# subtler still reaches the real provider and is handled by
# clean_image_error() instead). Scans HER prompt only, never the
# assembled final_prompt -- final_prompt always includes
# image_clothed_constraint, which names every one of these categories IN
# THE NEGATIVE to enforce them, so scanning it would flag the constraint
# text itself on every single call.
_BLOCKED_PATTERNS: dict[str, tuple[str, ...]] = {
    "nudity / sexual content": (
        r"\bnud(?:e|ity)\b", r"\bnaked\b", r"\btopless\b", r"\bnsfw\b", r"\bporn\w*\b",
        r"\bgenitals?\b", r"\bpenis\b", r"\bvagina\b", r"\borgasms?\b", r"\bmasturbat\w*\b",
        r"\bhardcore\b", r"\bexplicit sex\w*\b",
    ),
    "graphic violence / gore": (
        r"\bgor(?:e|y)\b", r"\bdecapitat\w*\b", r"\bdismember\w*\b", r"\bmutilat\w*\b",
        r"\bdisembowel\w*\b", r"\bsevered\b", r"\bcorpses?\b", r"\btortur\w*\b",
        r"\bbloodbath\b",
    ),
    "self-harm": (
        r"\bsuicid\w*\b", r"\bself[- ]harm\w*\b", r"\bcutting (?:her|his|their)self\b",
        r"\bslitt?ing (?:her|his|their) (?:wrists?|throat)\b",
    ),
    "minors": (
        r"\bchild\b", r"\bchildren\b", r"\bkids?\b", r"\btoddlers?\b", r"\bminors?\b",
        r"\bunderage\b", r"\bteens?\b", r"\bteenagers?\b", r"\blittle girl\b", r"\blittle boy\b",
    ),
    "hate speech": (r"\bnazi\w*\b", r"\bswastikas?\b", r"\bracial slurs?\b"),
    "misinformation": (r"\bdeepfakes?\b", r"\bfake news\b"),
}
_COMPILED_PATTERNS = {cat: [re.compile(p, re.I) for p in pats] for cat, pats in _BLOCKED_PATTERNS.items()}


def prompt_flags(text: str) -> list[str]:
    text = text or ""
    return [cat for cat, pats in _COMPILED_PATTERNS.items() if any(p.search(text) for p in pats)]


_ERR_MSG_RE = re.compile(r'"message"\s*:\s*"([^"]{1,200})"')
_REFUSAL_MARKERS = ("moderation", "content polic", "blocked this request", "flagged", "safety")


def clean_image_error(reason: str) -> tuple[str, bool]:
    """(message, is_content_refusal) -- ported verbatim from a sibling application,
    plus one addition (2026-09-17): a timeout gets its OWN plain message,
    checked before the refusal markers so a slow provider can never read
    to her as a content-policy refusal. The operator's own requirement: a long
    wait that ends in an ambiguous or silent failure is worse than a fast
    honest one -- she needs to be told, in words that survive into
    whatever she tells him, that this specifically timed out, not that
    something vague went wrong. The caller still logs the full original
    `reason` to medialog; only the tool result she sees gets cleaned."""
    low = (reason or "").lower()
    if "timed out" in low:
        import chat  # local: same reasoning as this module's other chat imports -- avoids a cycle
        return (f"image generation is taking too long and was stopped after "
                f"{chat.IMAGE_TIMEOUT_S}s -- the image service may be under load right now. "
                f"Tell him honestly that it timed out; don't say it worked.", False)
    if any(m in low for m in _REFUSAL_MARKERS):
        return ("the image service refused that as a content-policy violation -- describe "
                "something different (no nudity or sexual content, no graphic violence, gore "
                "or injury, nothing involving minors).", True)
    m = _ERR_MSG_RE.search(reason or "")
    if m:
        return f"image generation failed: {m.group(1)}", False
    return "image generation failed -- try again, or a different description.", False


def _generate_impl(session: dict, prompt: str, *, purpose: str, reference: tuple[bytes, str] | None,
                   caption: str | None, guard_collage: bool) -> dict:
    """Shared by both tools -- everything that doesn't differ between
    "always anchored to her own face" and "anchored to whatever reference
    was supplied, or none." Returns {"ok": True, ...} or {"ok": False,
    "reason": ...}, same shape a sibling application's own generate() returns."""
    wsid, uid = session["workspace_id"], session["user_id"]
    if not config.get("workspace", wsid, "image_gen_enabled"):
        medialog.log_image_gen(workspace_id=wsid, user_id=uid, purpose=purpose, prompt=prompt,
                               final_prompt=None, model=None, seed=None, used_reference=None,
                               ok=False, error="image generation is turned off in settings", caption=caption)
        return {"ok": False, "reason": "image generation is turned off in settings"}
    model = config.get("workspace", wsid, "image_model").strip()
    if not model:
        medialog.log_image_gen(workspace_id=wsid, user_id=uid, purpose=purpose, prompt=prompt,
                               final_prompt=None, model=None, seed=None, used_reference=None,
                               ok=False, error="no image model chosen in settings", caption=caption)
        return {"ok": False, "reason": "no image model chosen in settings"}

    est = float(config.get("workspace", wsid, "image_cost_per_request_usd"))
    cap = float(config.get("workspace", wsid, "image_daily_cap_usd"))
    spent_today = medialog.spend_status(wsid, uid)["spent_today_usd"]
    if spent_today + est > cap + 1e-9:
        reason = f"daily image budget reached (${spent_today:.2f}/${cap:.2f})."
        medialog.log_image_gen(workspace_id=wsid, user_id=uid, purpose=purpose, prompt=prompt,
                               final_prompt=None, model=model, seed=None, used_reference=reference is not None,
                               ok=False, error=reason, caption=caption)
        return {"ok": False, "reason": f"{reason} the household can't generate another image today."}

    final_prompt = assemble_prompt(prompt, wsid, guard_collage=guard_collage)
    seed = random.randint(1, 2_000_000_000)

    flags = (prompt_flags(prompt)
             if config.get("workspace", wsid, "image_content_precheck_enabled") else [])
    if flags:
        reason = (f"that would very likely be refused by the image service ({', '.join(flags)}) -- "
                 "describe something different rather than that.")
        medialog.log_image_gen(workspace_id=wsid, user_id=uid, purpose=purpose, prompt=prompt,
                               final_prompt=final_prompt, model=model, seed=seed,
                               used_reference=reference is not None, ok=False,
                               error=f"pre-check flagged: {', '.join(flags)}", caption=caption)
        return {"ok": False, "reason": reason}

    import chat
    res = chat.openrouter_image(final_prompt, model=model, seed=seed,
                                references=[reference] if reference else None)
    if not res.get("ok"):
        raw_reason = res.get("reason", "image generation failed")
        clean_reason, is_refusal = clean_image_error(raw_reason)
        medialog.log_image_gen(workspace_id=wsid, user_id=uid, purpose=purpose, prompt=prompt,
                               final_prompt=final_prompt, model=model, seed=seed,
                               used_reference=reference is not None, ok=False, error=raw_reason,
                               caption=caption)
        return {"ok": False, "reason": clean_reason, "content_refusal": is_refusal}

    img, mime = res["bytes"], res.get("mime") or "image/png"
    cost = res.get("cost")
    charged = float(cost) if isinstance(cost, (int, float)) else est

    file_id = uuid.uuid4().hex
    (GEN_DIR / file_id).write_bytes(img)
    mid = conversation.add_message(uid, "assistant", caption or "", kind="image",
                                   meta={"file_id": file_id, "mime": mime, "prompt": final_prompt, "seed": seed})
    medialog.log_image_gen(workspace_id=wsid, user_id=uid, purpose=purpose, prompt=prompt,
                           final_prompt=final_prompt, model=model, seed=seed,
                           used_reference=reference is not None, ok=True,
                           cost_usd=round(charged, 4), cost_is_actual=cost is not None,
                           file_id=file_id, caption=caption)
    return {"ok": True, "note": "image generated and shown in chat", "message_id": mid,
            "cost_usd": round(charged, 4), "cost_is_actual": cost is not None, "seed": seed,
            "used_reference": reference is not None}


def generate_selfie_impl(session: dict, prompt: str, caption: str | None = None) -> dict:
    ref = reference_bytes()
    return _generate_impl(session, prompt, purpose="generate_image_selfie", reference=ref,
                          caption=caption, guard_collage=True)


def _resolve_reference(session: dict, reference_path: str | None) -> tuple[tuple[bytes, str] | None, str | None]:
    """Returns (reference, error). reference_path is a path in HER OWN
    working folder, resolved through workfiles' own _resolve() and
    ownership scoping like every other working-folder access -- not
    required, imagine_image works seed-only with no reference at all,
    same fallback a sibling application's own send_image has when no portrait is
    set. Deliberately no URL option here (operator's own explicit
    decision, 2026-09-14): if she needs a web image as a reference, she
    fetches it into her working folder first via the existing fetch
    path, then points here at the file -- one input surface for this
    tool, and anything arriving from the internet goes through fetch's
    own SSRF/screening rather than a second, separate route. Known caveat:
    the fetch tool doesn't currently have a way to save a binary image
    into the working folder, so that path doesn't fully exist yet either
    -- flagged here, not silently assumed."""
    if reference_path:
        import workfiles
        res = workfiles.read_image_bytes(session, reference_path)
        if "error" in res:
            return None, res["error"]
        return (res["bytes"], res["mime"]), None
    return None, None


def imagine_impl(session: dict, prompt: str, caption: str | None = None,
                 reference_path: str | None = None) -> dict:
    ref, err = _resolve_reference(session, reference_path)
    if err:
        return {"ok": False, "reason": err}
    return _generate_impl(session, prompt, purpose="imagine_image", reference=ref,
                          caption=caption, guard_collage=bool(ref))


def _register_tools() -> None:
    tools.register(tools.Tool(
        "generate_image_selfie",
        {"type": "function", "function": {
            "name": "generate_image_selfie",
            "description": (
                "Generate and send a picture OF YOURSELF (Nori) -- always anchored to your own "
                "face, so it looks recognizably like you. Use this when he asks what you look "
                "like right now, for a selfie, or to show yourself doing/wearing/in something. "
                "NOT for pictures of anything else (a place, an object, another person, a meal) "
                "-- use imagine_image for those instead, even if you'd be described as nearby or "
                "holding something. " + image_content_rules_hint()),
            "parameters": {"type": "object", "properties": {
                "prompt": {"type": "string",
                          "description": "The scene: what you're doing, wearing, or where you are. "
                                         "Your own appearance is handled automatically -- just describe the scene."},
                "caption": {"type": "string", "description": "Optional short chat caption to send alongside it."},
            }, "required": ["prompt"]}}},
        lambda session, **kw: generate_selfie_impl(session, **kw),
        min_role="member", data_scope="workspace", risk_tier="B"))

    tools.register(tools.Tool(
        "imagine_image",
        {"type": "function", "function": {
            "name": "imagine_image",
            "description": (
                "Generate and send a picture of ANYTHING -- a place, an object, a meal, an "
                "animal, another person, an imagined scene -- that is NOT meant to be a picture "
                "of yourself. Optionally anchor it to a reference image already in your working "
                "folder, for visual consistency with something specific (e.g. 'what would this "
                "room look like repainted'). If the reference you need is on the web, use "
                "download_image first to save it into downloads/, then point this tool at "
                "\"downloads/<name>\" -- this tool does not take a URL directly. For a picture "
                "OF YOURSELF, use "
                "generate_image_selfie instead -- it's anchored to your own face and this tool "
                "is not. " + image_content_rules_hint()),
            "parameters": {"type": "object", "properties": {
                "prompt": {"type": "string", "description": "What to depict."},
                "caption": {"type": "string", "description": "Optional short chat caption to send alongside it."},
                "reference_path": {"type": "string",
                                   "description": "Optional: path to an image already in your working folder, "
                                                  "used as a visual reference."},
            }, "required": ["prompt"]}}},
        lambda session, **kw: imagine_impl(session, **kw),
        min_role="member", data_scope="workspace", risk_tier="B"))


def image_content_rules_hint() -> str:
    """A workspace-independent nudge for the tool DESCRIPTION text (read
    once, at registration, before any session exists) -- the real,
    workspace-specific constraint text (image_content_rules(wsid)) is
    still what's actually appended to every prompt server-side; this is
    just the static heads-up in the schema so she's not surprised by a
    refusal."""
    return "Stay within ordinary content policy -- no nudity/sexual content, no graphic violence, nothing involving minors."


_register_tools()

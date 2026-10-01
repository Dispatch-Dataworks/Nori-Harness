# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Generic per-user/per-workspace settings -- the only module with raw SQL
against `settings`. Built now (Phase 9) because the proactive scheduler
needs somewhere to keep per-user ping preferences before a real settings
UI exists (Phase 10 reuses this table and this module as-is, rather than
replacing it).

── Spec shape (2026-09-14, widened for Nori's own read access) ─────────
Each entry is (default, type, scope, secret, label):
  - default, type: unchanged, as before.
  - scope: 'user' or 'workspace' -- NOW encoded here rather than left to
    each caller (the original design's own stated weakness: "nothing
    stops a caller from getting/setting a genuinely per-user key at
    workspace scope"). Every existing call site was audited against this
    (grepped every config.get/set in the app) and matches exactly what's
    declared here; get()/set() still take scope as an explicit argument
    for backward compatibility with those ~40 call sites, this is not
    (yet) cross-checked against them, just the one place that now also
    lets a NEW caller -- settings_tool.py's read-everything tool -- look
    up the right scope per key without a second, driftable map of its
    own.
  - secret: REQUIRED, not defaulted -- every entry must say plainly
    whether it holds something secret. This is the actual point of this
    change (operator's own explicit ask): settings_tool.get_settings
    below reads readable_spec(), which excludes anything secret=True, so
    a future setting is excluded from her read access unless someone
    positively says otherwise, structurally, in the one place its shape
    is already declared -- never a second denylist that could quietly
    drift out of sync with what actually got added here. Nothing in this
    spec IS secret today (real secrets in this app live as env vars or
    dedicated encrypted columns -- sub_agents.api_key_enc, peers.psk_enc
    -- never in this table); the flag exists so the NEXT thing added here
    doesn't become an accident.
  - label: a short, human phrase for display -- settings_tool.py's read
    tool shows this instead of (or alongside) the raw key, since "shown
    the operator's own settings" is more useful to her as "ping window
    end (local hour): 22" than as "ping_window_end: 22". Purely
    cosmetic, unlike scope/secret -- a missing label just falls back to
    the raw key, never a safety concern, so it's fine for this one to be
    filled in loosely.
"""
from __future__ import annotations

import json
import time

import store

_SPEC: dict[str, tuple] = {
    # Single source of truth for "what timezone is he in" (2026-09-18,
    # real bug: every site that needed local time used to call
    # time.localtime()/mktime(), implicitly trusting the SERVER's own OS
    # clock -- correct for him only by coincidence of where this happens
    # to run). A real IANA name (usertime.py validates it), not an offset
    # -- an offset alone can't self-correct across a DST transition.
    # Defaults to what his own connected Google Calendar reports
    # (America/New_York) -- auto-filled at connect time if this is still
    # unset (see server.py's oauth_callback_get), never overwritten once
    # he's touched it himself.
    "timezone": ("America/New_York", str, "user", False, "your timezone (IANA name, e.g. America/New_York)"),
    "ping_enabled": (True, bool, "user", False, "proactive check-ins enabled"),
    "ping_window_start": (8, int, "user", False, "ping window start (local hour)"),
    "ping_window_end": (22, int, "user", False, "ping window end (local hour)"),
    "ping_min_gap_min": (90, int, "user", False,
                        "skip a proactive message if active this recently (minutes)"),
    # The email ping signal's own cadence (2026-09-26, the operator: "email check
    # frequency is a config option, defaulting to 4x daily... spread
    # those four across his waking hours, not every six hours around the
    # clock"). A plain count, not an interval, because that's how he
    # expressed it and what he'll want to adjust -- email_calendar.py's
    # own _email_check_interval_seconds() divides his real ping_window
    # span by this count, so 4 checks fall within the hours he's actually
    # awake, never wasted overnight. The cap/estimate are the same
    # shape imagegen.py's own image_daily_cap_usd/image_cost_per_request_usd
    # already use -- once hit, the email signal SKIPS for the rest of the
    # day (never borrows from anything else), and every skip is a real,
    # visible row on the cost page (ping_signal_spend), not a silent stop.
    "ping_signal_email_checks_per_day": (4, int, "user", False, "email nag: checks per day, spread across your ping window"),
    "ping_signal_email_daily_cap_usd": (0.25, float, "user", False, "email nag: daily spend cap (USD)"),
    "ping_signal_email_cost_estimate_usd": (0.02, float, "user", False, "email nag: estimated cost per check (USD), used before a real figure is known"),
    # off by default, same reasoning as a sibling application's vision_chat_enabled --
    # enabling this sends images from your own working folder to a 3rd
    # party vision model. See workfiles.py.
    "workfile_vision_enabled": (False, bool, "user", False, "working-folder images sent to vision model"),
    # Separate from workfile_vision_enabled on purpose (2026-09-15, ported
    # from a sibling application's identical vision_chat_enabled) -- a casual photo
    # sent directly in chat is a different privacy/cost surface than a
    # working-folder file she was asked to look at, and someone may
    # reasonably want one on without the other. Off by default, same
    # reasoning as workfile_vision_enabled: enabling this sends a photo
    # you send her to a 3rd party vision model.
    "chat_vision_enabled": (False, bool, "user", False, "photos sent in chat sent to vision model"),
    # on by default -- a small muted line in chat when she calls a tool
    # (name only, never arguments or results). See conversation.py's
    # include_tool param and chat.py's run() for where this is read.
    "show_tool_calls": (True, bool, "user", False, "show tool-call lines in chat"),
    # Poll-driven local notifications (see server.py's PWA/notify section)
    # -- not push, no VAPID, honest about that in the settings copy.
    "notify_enabled": (True, bool, "user", False, "local notifications enabled"),
    "notify_quiet_start": (23, int, "user", False, "notification quiet hours start (local hour)"),
    "notify_quiet_end": (8, int, "user", False, "notification quiet hours end (local hour)"),
    # Per-turn cap on tool-calling rounds before she must produce final
    # text (see chat.py's run()). Used to be one hardcoded, restart-only
    # env var (NORI_MAX_TOOL_ROUNDS=4) -- real logs showed every hit of it
    # was a genuine multi-step task (an MCP search -> get -> update chain),
    # not a runaway loop, so it was raised and split in two: a live chat
    # turn has someone watching who'll notice and can just ask her to keep
    # going, so it can be generous; an unattended turn (a proactive ping, a
    # peer nudge, force_checkin's background thread) has nobody there to
    # notice a stuck loop, which is exactly the runaway-cost scenario this
    # cap exists for, so it stays conservative by default.
    "tool_rounds_chat": (12, int, "user", False, "max tool-calling rounds per live chat turn"),
    # Raised 4 -> 8 (2026-09-19, operator's own ask): trust_level=full on a
    # peer connection reinstates the 11 tools peer_trust_gate otherwise
    # hides on a peer-motivated turn (calendar, contacts, Drive,
    # SharePoint) -- a realistic "check the peer's message, check a
    # tool or two for context, send" sequence already needs 4-5 rounds
    # minimum, leaving no slack at the old cap. Still well short of
    # tool_rounds_chat's 12: nobody is watching an unattended turn to
    # notice a runaway loop, so some asymmetry stays deliberate. Not
    # just a tuning nicety -- see peer_turn_limit_log (store.py) and
    # peers._run_prompted_turn's own new logging: hitting this cap on a
    # peer-motivated turn used to be mechanistically indistinguishable
    # from the model genuinely choosing not to reply, since the
    # round-limit fallback text is discarded exactly like any other
    # unsent reply on that kind of turn.
    "tool_rounds_proactive": (8, int, "user", False, "max tool-calling rounds per unattended turn"),
    # Recent peer-context awareness (2026-09-14, operator's own ask) --
    # DISTINCT from pending_delivery_messages()'s own "unprocessed, ride
    # along and prompt a decision" mechanism -- this is read-only
    # background on already-handled received messages, folded into every
    # turn's system prompt the same way memory is. See
    # peers.recent_context_block()'s own docstring for the full reasoning.
    # Widened 3->12h / 3->5 (2026-09-19, operator's own ask): a message
    # used to go from vividly present (unread, injected every ordinary
    # turn -- see the include_peer_pending call sites below) to entirely
    # gone within 3 hours or 3 messages the instant it was marked read --
    # a real, jarring cliff, not a gradual fade. 5 is now PER PEER, not
    # shared across every connected peer combined (see
    # peers.recent_context_block()'s own rewritten docstring) -- a second,
    # independent fix, since a shared cap let one chatty peer evict
    # another's messages regardless of the window.
    "peer_recent_cap": (5, int, "workspace", False,
                       "recent peer messages surfaced per turn (per peer, not shared across peers)"),
    "peer_recent_window_hours": (12, int, "workspace", False, "recent peer message window (hours)"),
    # The admin peer-debug panel's own display cap (2026-09-14, operator's
    # own ask) -- separate from peer_recent_cap above: that one bounds what
    # rides into a live turn's own context, this one bounds what a human
    # admin sees at a glance on /admin/peers. Newest-first, capped here;
    # anything older is still reachable via that panel's own "full log"
    # link, never silently unreachable.
    "peer_debug_limit": (20, int, "workspace", False, "peer messages shown per peer on the admin debug panel"),
    # Image generation (2026-09-14, operator's own ask: mirror a sibling application's
    # send_image, as two distinct tools -- generate_image_selfie and
    # imagine_image, see imagegen.py). Workspace-scoped throughout, one
    # shared configuration and one shared spend budget for the whole
    # household -- deliberate, not an oversight (see imagegen.py's own
    # module docstring for the reasoning).
    "image_gen_enabled": (False, bool, "workspace", False, "let her generate and send images"),
    "image_content_precheck_enabled": (True, bool, "workspace", False,
                                       "check image prompts locally before generation"),
    "image_model": ("bytedance-seed/seedream-5-0-lite", str, "workspace", False,
                    "image generation model (OpenRouter)"),
    # {appearance} is NOT a template placeholder here the way a sibling application's
    # equivalent uses one -- her physical description is baked directly
    # into this string rather than kept as a second, separately-derived
    # config value (a sibling application's own appearance_description, and the real
    # vision call that derives it from a reference photo, aren't ported --
    # not asked for, and the reference IMAGE below already does the actual
    # identity-anchoring work; a parallel text description is reinforcement
    # a sibling application has and this doesn't need to duplicate).
    "image_style_prefix": (
        "Photograph of the same woman in every image: a woman in her late 20s to early "
        "30s, dark brown wavy hair often worn up in a loose bun, brown eyes, warm light-tan "
        "skin with a few light freckles, average build. She dresses neatly but casually -- "
        "a blazer or cardigan, a simple top, everyday clothes suited to someone helping run "
        "a household, not a uniform. Natural, well-lit photography, candid but composed "
        "framing -- like an ordinary photo taken during the day, not a staged studio shoot "
        "or a stock image.", str, "workspace", False, "image style prefix"),
    # Same content-safety text as a sibling application's image_clothed_constraint,
    # age clause adjusted to match the actual reference photo -- this is
    # the belt-and-braces layer appended after whatever she writes AND the
    # text surfaced to her directly via imagegen.image_content_rules()
    # (tools.capabilities_block-equivalent), one value so the two can't
    # drift apart.
    "image_clothed_constraint": (
        "She is fully clothed at all times. No nudity, no sexual or suggestive content of "
        "any kind. No graphic violence, gore, blood, or injury. Nothing depicting or "
        "implying a minor -- she is a woman in her late 20s to early 30s, always shown as "
        "a clearly adult woman.", str, "workspace", False, "image content constraint"),
    "image_daily_cap_usd": (1.0, float, "workspace", False,
                            "daily image spend cap (USD), shared by both image tools"),
    "image_cost_per_request_usd": (0.035, float, "workspace", False,
                                   "per-image cost estimate (USD), used when the provider "
                                   "doesn't return a real figure"),
    # Context-tuning pane (2026-09-14, operator's own ask: since every
    # message is stored regardless, let him tune the balance between
    # active and summarized context directly, rather than us picking
    # numbers). These replace what were previously restart-only env vars
    # -- same defaults, now live-editable, workspace-scoped like every
    # other setting on this page.
    "context_window_msgs": (30, int, "workspace", False, "live conversation window (messages)"),
    "compaction_enabled": (True, bool, "workspace", False, "older messages summarized into context"),
    "compaction_max_segments": (8, int, "workspace", False, "max compacted segments kept live"),
    "compaction_budget_tokens": (500, int, "workspace", False, "compacted-summary token budget"),
    "compaction_session_gap_hours": (2.0, float, "workspace", False,
                                     "gap that starts a new compaction session (hours)"),
    "memory_max_tokens": (375, int, "workspace", False, "memory context token budget"),
    "memory_pinned_max_tokens": (150, int, "workspace", False, "pinned-memory token budget"),
    # web_fetch write mode (2026-09-14, operator's own explicit safety
    # design) -- 'off' (default: closed, matching the read/write asymmetry
    # webtools.py already documents), 'simulated' (logged, never actually
    # sent, a clear synthetic result returned instead of a real one), or
    # 'real'. Enforced as a two-step transition in set() below -- off
    # can't jump straight to real -- so turning writes on for real is a
    # deliberate second act, not a single toggle.
    "web_fetch_write_mode": ("off", str, "workspace", False, "web fetch write mode (off/simulated/real)"),
    # Separate on/off for the two web tools (2026-09-15, operator's own
    # ask) -- web_search and web_fetch never had a gate of their own
    # before this; only a Tavily key's presence implicitly decided
    # whether search worked, and fetch had no gate at all besides the
    # write-mode above (which only governs non-GET requests, not whether
    # fetch runs at all). True by default -- both already worked
    # unconditionally before this, so the default preserves that; this
    # only adds the ability to turn either off, not a behavior change on
    # deploy. Checked inside _web_search_impl/_web_fetch_impl themselves
    # (webtools.py), same place image_gen_enabled is checked inside
    # _generate_impl -- not at tool-registration time, so no restart is
    # needed for a toggle to take effect.
    "web_search_enabled": (True, bool, "workspace", False, "let her search the live web"),
    "web_fetch_enabled": (True, bool, "workspace", False, "let her fetch the content of a URL"),
    # default_model_slug removed 2026-09-30 -- superseded by model_chain
    # (see models.py/providers.py): primary+fallback are now a real
    # ordered table, not a single scalar setting, and there's deliberately
    # no default value anymore. providers.migrate_from_env() reads any
    # pre-existing value straight from the settings table (not via this
    # spec, which no longer has an entry for it) as a one-time upgrade step.
    # Voice layer (2026-09-12; reworked into a real voice-conversation
    # mode 2026-09-13 -- see server.py's voice-modal block). tts_voice is
    # a gpt-4o-mini-tts preset name (see voice.py's VOICES) -- a setting,
    # not a constant, because the operator picked "nova" by ear across
    # the real roster and will want to try others.
    # Not secret -- a preset NAME, never a key -- but worth naming
    # explicitly here rather than leaving a reader to guess.
    # voice_input_enabled off by default, same discipline as
    # workfile_vision_enabled -- it gates a mic button that opens
    # push-to-talk voice mode, which sends a real recording of you to
    # OpenAI's speech-to-text API and gets her replies spoken back via
    # OpenAI's text-to-speech API, both plainly said in the settings copy
    # next to this toggle, not something that should just be on. There is
    # no separate autoplay setting any more -- inside voice mode, every
    # reply is spoken; outside it, nothing is (no per-message play button
    # either, since it was replaced by this whole mode).
    "tts_voice": ("nova", str, "user", False, "voice preset"),
    "voice_input_enabled": (False, bool, "user", False, "push-to-talk voice mode enabled"),
    # Per-turn timing instrumentation (2026-09-13, see timing.py) -- off by
    # default, workspace-scoped (an ops/debug toggle, not a personal
    # preference; nothing stops it being flipped per-user in practice
    # since there's usually one workspace, but it reads as "trace turns
    # for this household" rather than "for this person"). One extra
    # SELECT per turn when off; a real, structured log line per turn when
    # on.
    "debug_timing_enabled": (False, bool, "workspace", False, "per-turn timing instrumentation enabled"),
    # A real off switch (2026-09-25, the operator: "I appreciate it but I can see how others would find it
    # annoying") -- when off, nothing emotion-related reaches her context, the UI, or the turn:
    # emotion.get_state() always reports the neutral default (so every consumer of it -- avatar
    # rendering, message tagging -- degrades to that for free, no second check needed anywhere else),
    # the precheck reminder line returns "" (precheck.build_block() already skips a check that adds
    # nothing, so the message-assembly slot just closes up, never a gap), and the set_emotion tool is
    # not offered at all (tools.Tool's own enabled=callable(session) mechanism, not a second registry).
    # Workspace-scoped, same reasoning as backup_enabled: instance-wide, not a personal preference.
    "emotion_enabled": (True, bool, "workspace", False, "her visible emotional state (avatar + set_emotion)"),
    # The operator's own name for THIS instance (2026-09-25, the operator: "the product is called Nori (Nori-Harness)
    # we just allow people to name and assign their own identity to their own instance"). Read fresh wherever
    # she's addressed by name -- persona.py's {{ASSISTANT_NAME}} substitution, context.py's name/time header,
    # server.py's UI strings, peers.py's outbound PACI agent_name -- never baked into a file or a stored row.
    # The harness/product identity (the repo, the PACI specification, server_version, the Nori-PACI User-Agent) is NOT
    # this setting and never reads it -- that's software identification, not the assistant's own name.
    "assistant_name": ("Nori", str, "workspace", False, "her name, as far as she and the UI are concerned"),
    # Pluggable memory backend (2026-09-25) -- "local" is the only one that exists today and the
    # only value this accepts; memory.py falls back to it for anything unrecognized rather than
    # erroring, same posture schema_for() already takes for a stale name. Exists now, ahead of a
    # real second backend, so the seam (memory.py's own LocalMemoryBackend class + this switch) is
    # exercised by something real before anything else is built against it.
    "memory_backend": ("local", str, "workspace", False, "which store her typed memory reads and writes through"),
    # The "nodrya" backend's own connection details -- only read when
    # memory_backend above is set to "nodrya". Workspace-scoped to match
    # memory_backend itself (backend selection and its connection are one
    # decision). Off by construction until all three are filled in: see
    # memory.py's NodryaMemoryBackend.
    #
    # nodrya_mcp_token is the one real exception to this module's own
    # "nothing in this spec is secret, real secrets live in dedicated
    # _enc columns" rule above -- there's no dedicated providers-style
    # table for it to live in, so the value server.py writes here is a
    # crypto.encrypt() token, not plaintext, and NodryaMemoryBackend
    # decrypts it on read. secret=True still matters on top of that --
    # it's what keeps this key out of settings_tool's read-everything
    # surface -- but don't let that flag alone imply it's stored in the
    # clear.
    "nodrya_mcp_url": ("", str, "workspace", False, "Nodrya MCP endpoint URL"),
    "nodrya_mcp_token": ("", str, "workspace", True, "Nodrya MCP token (write scope, encrypted at rest)"),
    "nodrya_memory_category_id": (0, int, "workspace", False, "Nodrya category id used for her memory"),
    # Backups (2026-09-18, both apps -- see backup.py's own module
    # docstring for the full design). Workspace-scoped: this is instance
    # infrastructure, not a personal preference. Off by default -- needs
    # NORI_BACKUP_KEY configured first (a real, separate secret, .env
    # only, never in this table) before it does anything real.
    "backup_enabled": (False, bool, "workspace", False, "daily backups enabled"),
    "backup_hour": (3, int, "workspace", False, "local hour backups run (0-23)"),
    "backup_retention_days": (14, int, "workspace", False, "days a local backup is kept before pruning"),
    # "" = local disk only, no cloud upload. Otherwise one of the
    # connected_accounts provider keys backup.py's _REMOTE_PROVIDERS
    # names -- not re-validated against that list here (config.py has no
    # cross-module import of backup.py), backup.py's own upload_one()
    # refuses an unrecognized value with a real error instead.
    "backup_remote_provider": ("", str, "workspace", False, "backup destination (blank = local only)"),
    "backup_sharepoint_site_id": ("", str, "workspace", False, "SharePoint site id (only used if destination is sharepoint)"),
    # Integration health checks (2026-09-19) -- extends PACI's own §4.1
    # liveness pattern to every other outward-facing integration (see
    # integration_health.py). Workspace-scoped, same reasoning as
    # debug_timing_enabled: this is instance operations, not a personal
    # preference. 5 minutes matches PACI's own default cadence -- "cheap
    # enough to run often" for a lightweight authenticated GET, same
    # judgment call, not re-derived. Tavily is the one exception (see
    # that module's own docstring) -- this interval governs it too for
    # config-only presence, but never triggers a real, credit-spending
    # search call on a timer.
    "integration_health_interval_minutes": (5, int, "workspace", False,
                                            "how often integrations are auto-checked (minutes)"),
}


def spec() -> dict:
    return _SPEC


def readable_spec() -> dict:
    """Every _SPEC entry NOT marked secret -- the single place Nori's own
    settings_tool.get_settings derives what it may read. A key added here
    later is excluded automatically unless its own entry says secret=False
    -- see the module docstring above for why that direction, and not a
    denylist, is the point."""
    return {k: v for k, v in _SPEC.items() if not v[3]}


def scope_for(key: str) -> str:
    if key not in _SPEC:
        raise KeyError(key)
    return _SPEC[key][2]


def _coerce(value, typ):
    if typ is bool:
        return value.strip().lower() in ("1", "true", "yes", "on") if isinstance(value, str) else bool(value)
    return typ(value)


def get(scope: str, scope_id: int, key: str):
    if key not in _SPEC:
        raise KeyError(key)
    default, typ = _SPEC[key][0], _SPEC[key][1]
    row = store.read(lambda c: c.execute(
        "SELECT v FROM settings WHERE scope=? AND scope_id=? AND key=?",
        (scope, scope_id, key)).fetchone())
    if row is None:
        return default
    try:
        return _coerce(json.loads(row["v"]), typ)
    except (ValueError, TypeError, json.JSONDecodeError):
        return default


_CONTEXT_BOUNDS = {
    "context_window_msgs": (1, 200), "compaction_max_segments": (0, 30),
    "compaction_budget_tokens": (0, 3000), "compaction_session_gap_hours": (0.25, 24),
    "peer_recent_cap": (1, 20), "peer_recent_window_hours": (0, 72),
    "memory_max_tokens": (50, 3000), "memory_pinned_max_tokens": (50, 2000),
}


def set(scope: str, scope_id: int, key: str, value) -> None:
    if key not in _SPEC:
        raise KeyError(key)
    typ = _SPEC[key][1]
    value = _coerce(value, typ)
    # Context-tuning pane (2026-09-14) -- bounds matching server.py's own
    # _CONTEXT_FIELDS, enforced here too rather than trusting the HTML
    # form's min/max alone: an experiment can adjust freely within these,
    # but can't wedge her into a genuinely unusable state.
    if key in _CONTEXT_BOUNDS:
        lo, hi = _CONTEXT_BOUNDS[key]
        if not lo <= value <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
    # web_fetch_write_mode's two-step transition (2026-09-14, operator's
    # own explicit ask: "turning writes on is a two-step deliberate act")
    # -- 'off' can only ever move to 'simulated' next, never straight to
    # 'real'. Checked against the CURRENTLY stored value, not the
    # request in isolation, so this is a real workflow constraint, not
    # just a default.
    if key == "web_fetch_write_mode":
        if value not in ("off", "simulated", "real"):
            raise ValueError("web_fetch_write_mode must be off, simulated, or real")
        current = get(scope, scope_id, key)
        if current == "off" and value == "real":
            raise ValueError("writes must pass through 'simulated' before 'real' -- "
                             "set simulated first, then real, as two separate deliberate steps")
    if key == "assistant_name":
        if not value.strip():
            raise ValueError("assistant_name cannot be empty")
        if len(value) > 60:
            raise ValueError("assistant_name must be 60 characters or fewer")
    now = time.time()
    def _w(c):
        row = c.execute("SELECT 1 FROM settings WHERE scope=? AND scope_id=? AND key=?",
                        (scope, scope_id, key)).fetchone()
        if row:
            c.execute("UPDATE settings SET v=?, updated_ts=? WHERE scope=? AND scope_id=? AND key=?",
                     (json.dumps(value), now, scope, scope_id, key))
        else:
            c.execute("INSERT INTO settings(scope, scope_id, key, v, updated_ts) VALUES (?,?,?,?,?)",
                     (scope, scope_id, key, json.dumps(value), now))
    store.write(_w)

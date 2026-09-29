# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Builds the system prompt for a turn: a short fixed operating-context
header, the persona, tool-usage guidance (mechanics, kept separate from
persona's character content), her current emotional state, and the
always-on memory slice (Phase 5). No capabilities block yet -- native
tool-calling schemas already tell the model what it can do; a a sibling application-
style behavioral-nudge layer on top is optional polish.

Per-user timezone/locale isn't wired up yet either (falls back to the
server's own local time) -- a real per-user preference belongs in typed
memory's `identity` type, which now exists but isn't auto-populated from
anywhere yet. Disclosed scope cut, not an oversight.
"""
from __future__ import annotations

import time

import accounts
import config
import conversation
import emotion
import guidance
import jobs
import memory
import persona

_OPS = """Your name is {assistant_name}. You're messaging {name} directly, one to one, in a private \
assistant app -- not a public or shared chat. It is {now} where the app is running. \
Everything after this is your persona and operating context, not lines to recite."""


def _system_parts(user_id: int, display_name: str) -> list[tuple[str, str]]:
    """(label, text) for every piece of build_system(), in order -- the
    ONE place that order/content is assembled. build_system() itself and
    composition_breakdown() (2026-09-14, context-tuning pane) both derive
    from this instead of each re-listing the same sections."""
    # Local import: mcp_servers -> ingest -> chat -> context would be a
    # real circular import at module-load time if this were a top-level
    # import instead -- deferred to call time, by which point every
    # module involved is already fully loaded, same reasoning
    # household.py/meals.py already use for their own late `import
    # scheduler`. peers -> ingest -> chat -> context is the identical
    # shape, same fix.
    import mcp_servers
    import peers

    now = time.strftime("%A %Y-%m-%d %H:%M")
    state = emotion.get_state(user_id)
    return [
        ("name/time header", _OPS.format(name=display_name, now=now,
                                         assistant_name=persona.assistant_name_for_user(user_id))),
        ("persona", persona.load_prompt(user_id)),
        ("guidance", guidance.load_prompt()),
        # The mechanism (only changes via set_emotion, persists otherwise) is
        # explained once, in guidance.md's own "Emotional state" section --
        # this line reports the current value, not the rule, so it isn't
        # restated here too.
        ("emotional state line", f"Your current emotional state is: {state}."),
        ("memory", memory.context_block(user_id)),
        ("jobs digest", jobs.digest_line(user_id)),
        ("mcp capabilities", mcp_servers.capability_block(user_id)),
        ("peer relationships", peers.capability_block(user_id)),
        ("recent peer exchanges", peers.recent_context_block(user_id)),
        # Pending peer content is deliberately NOT here (2026-09-13,
        # operator's own correction) -- it used to be, folded into this
        # standing system prompt, but proximity is what makes context get
        # used (precheck.py's own real lesson, not just an opinion) and a
        # peer's own identity/relationship framing sitting several
        # sections above raw thread content, buried in a big standing
        # block, is the opposite of proximate. See
        # peers.pending_delivery_messages() and build_messages() below --
        # each peer's framing+content now travels together as its OWN
        # late, separate message, not string-concatenated in here.
    ]


def build_system(user_id: int, display_name: str) -> str:
    parts = [text for _label, text in _system_parts(user_id, display_name)]
    return "\n\n".join(p for p in parts if p.strip())


def build_messages(user_id: int, display_name: str, *, peer_pending: str | None = None) -> list[dict]:
    """The full messages list ready for the chat-completions call: system
    prompt, then any compacted older sessions (oldest first), then the
    live raw window, oldest first, then (if peer_pending is set) each
    peer's own pending-content message, one per peer with something
    unread FOR THAT DIMENSION -- one continuous chronological sequence,
    most-recent/most-proximate content last. Compacted segments are
    inserted here, at the position the real messages they replace used to
    occupy, deliberately NOT folded into build_system()'s own standing
    block above -- see compaction.py's module docstring for why position
    matters. Pending peer content follows the identical reasoning
    (2026-09-13) -- see peers.pending_delivery_messages()'s own docstring,
    including its 2026-09-19 split into two independent dimensions."""
    import compaction  # local: same reasoning as mcp_servers/peers below -- avoids a load-order cycle
    import peers
    msgs = [{"role": "system", "content": build_system(user_id, display_name)}]
    for seg in compaction.segments_for_context(user_id):
        msgs.append({"role": "system",
                    "content": "[Auto-summary of earlier conversation -- generated, not verbatim]\n"
                              + seg["text"]})
    # context_window_msgs (2026-09-14, context-tuning pane) -- workspace-
    # scoped, replacing conversation.py's own former DEFAULT_WINDOW
    # constant; read live so a change applies to her very next reply.
    user = accounts.get_user(user_id)
    window_msgs = config.get("workspace", user["workspace_id"], "context_window_msgs") if user else conversation.DEFAULT_WINDOW
    for m in conversation.recent(user_id, limit=window_msgs):
        text = conversation.render_for_model(m)
        if text or m["role"] != "assistant":     # an assistant line that was only an artefact renders as nothing: leave it out rather than send an empty turn
            msgs.append({"role": m["role"], "content": text})
    # peer_pending's default stays None (withheld) for any caller that
    # doesn't explicitly ask (2026-09-13, tightened same day -- see
    # chat.run()'s own docstring for the real gap defaulting True left
    # open). Two independent dimensions as of 2026-09-19 -- "peer" for a
    # peer-motivated turn, "user" for every turn answering the operator
    # directly, live or via orphan-sweep (his own explicit answer to
    # "inject unread peer content into every turn from me") -- a message
    # already shown to one dimension is NOT thereby read for the other;
    # peers.pending_delivery_messages() tracks and stamps them separately.
    # Only a turn nobody asked for -- a proactive ping, a schedule, a
    # reminder -- still gets both withheld by default. Nothing is marked
    # presented when withheld -- the pending message simply waits for the
    # next turn of that same kind that DOES want it, never lost, never
    # expired by sitting unread.
    if peer_pending:
        msgs.extend(peers.pending_delivery_messages(user_id, dimension=peer_pending))
    return msgs


def _est_tokens(s: str) -> int:
    return len(s) // 4


def composition_breakdown(session: dict, user_id: int, display_name: str) -> dict:
    """Real token counts for a live turn's actual assembled prompt, by
    section -- the measurement half of the context-tuning pane
    (2026-09-14, operator's own ask: "show the resulting composition...
    so he can see persona's share change as he adjusts"). Same idea as
    a sibling application's identical function -- see that one's own docstring; this
    additionally folds in nori's per-turn precheck block (emotion +
    pinned facts, both registered checks combined into one message by
    precheck.build_block()), which a sibling application's simpler single-check
    version already covers by its own equivalent call.

    Calls chat.call() for NOTHING -- assembles the same text
    build_system()/build_messages() would send, estimates tokens the
    same way (_est_tokens, chars//4). Returns {"sections": [...],
    "total_tokens": int}, sections in actual prompt order."""
    import precheck
    rows = [{"label": label, "tokens": _est_tokens(text), "chars": len(text)}
           for label, text in _system_parts(user_id, display_name)]
    import compaction  # local: same reasoning as build_messages()'s own
    compaction_tok = compaction_chars = 0
    for seg in compaction.segments_for_context(user_id):
        text = "[Auto-summary of earlier conversation -- generated, not verbatim]\n" + seg["text"]
        compaction_tok += _est_tokens(text)
        compaction_chars += len(text)
    rows.append({"label": "compaction summaries", "tokens": compaction_tok, "chars": compaction_chars})
    user = accounts.get_user(user_id)
    window_msgs = config.get("workspace", user["workspace_id"], "context_window_msgs") if user else conversation.DEFAULT_WINDOW
    convo_tok = convo_chars = 0
    for m in conversation.recent(user_id, limit=window_msgs):
        text = conversation.render_for_model(m)
        convo_tok += _est_tokens(text)
        convo_chars += len(text)
    rows.append({"label": "raw conversation window", "tokens": convo_tok, "chars": convo_chars})
    checklist = precheck.build_block(session, user_id)
    if checklist:
        rows.append({"label": "precheck (proximate, last -- emotion + pins)",
                    "tokens": _est_tokens(checklist["content"]), "chars": len(checklist["content"])})
    total = sum(r["tokens"] for r in rows)
    for r in rows:
        r["pct"] = round(r["tokens"] / total * 100, 1) if total else 0.0
    return {"sections": rows, "total_tokens": total}

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Conversation compaction -- tiered context instead of a hard cutoff.
Recent messages ride along verbatim (conversation.recent(), unchanged);
everything older gets partitioned into real conversational sessions and
summarized, one segment at a time, anchored to the exact message-id span
it covers.

Built 2026-09-12, replacing "older than N messages simply doesn't exist"
(store.py's own prior comment on `messages`). The design choices below
come directly out of two things found the same day, not from theory:

  - A segment is NEVER regenerated from a prior segment's own summary
    text -- only ever from conversation.in_range()'s raw messages. This
    is the direct fix for a real, observed bug: a sibling application's old rolling
    summarizer fed its own prior output back in as input every cycle,
    and a live spot-check found it had genuinely misattributed a theme
    from one time window into its account of the next one, plus a
    stretch of "Unresolved: ..." boilerplate that survived unchanged
    across four rewrites regardless of what actually happened in
    between. Re-deriving every segment from its own untouched raw span
    makes that class of drift structurally impossible, not just less
    likely.
  - Segment summaries are injected INLINE, in chronological order,
    where the messages they replace used to sit -- never folded into
    the standing, position-0 system-prompt block. Same lesson as
    precheck.py: this project found twice now (emotion, then here) that
    what actually gets used is what's positioned right, not just worded
    right.

Session boundaries reuse conversation.py's own search_history reasoning
(a real time gap means a different sitting, not more context on the
same one) at a deliberately looser threshold than search's own 30
minutes -- that number was tuned to avoid contaminating a SPECIFIC
match's context, a tighter bar than "is this the same sitting" needs.
"""
from __future__ import annotations

import time

import accounts
import chat
import config
import conversation
import store

# Session-gap threshold, max segment count, and context token budget
# (2026-09-14, context-tuning pane) moved to config.py, workspace-scoped
# -- read live below instead of cached as module constants, so a change
# applies without a restart. Same defaults these used to be as env vars.

# The structural facts about compaction that AREN'T one of the tunable
# numbers above -- those already have a real source (config.readable_spec());
# this is the one for the two behavioral invariants from the module
# docstring, hoisted into a real constant (2026-09-15) so a self-knowledge
# tool can quote this instead of maintaining its own paraphrase that could
# drift from what's actually true here.
COMPACTION_EXPLAIN = (
    "Recent messages ride along verbatim; anything older is partitioned into real "
    "conversational sessions and summarized, one segment at a time. A segment is never "
    "regenerated from a prior segment's own summary text -- only ever from the original raw "
    "messages it covers, so a paraphrase can't compound into drift across rewrites. Segment "
    "summaries are injected inline, in chronological order, exactly where the messages they "
    "replace used to sit -- never folded into the standing system prompt, since what actually "
    "gets used is what's positioned right, not just worded right."
)

# Per-segment summary length: target ~13% of the segment's own raw size,
# floored so a short segment doesn't get an absurdly clipped summary and
# ceilinged so one long day doesn't bloat back into the noise this exists
# to reduce. Generation-time verbosity, not a context-composition knob --
# left as plain constants, out of the tuning pane's scope.
SUMMARY_MIN_TOKENS = 40
SUMMARY_MAX_TOKENS = 300
SUMMARY_TARGET_RATIO = 0.13

_SUMMARIZE_SYS = """Summarize this stretch of a real conversation between {name} and their \
assistant into a tight paragraph, no more than {n} words. Preserve: what was decided or asked \
for, anything unresolved, and anything said explicitly that a later reply might need to refer \
back to. Drop small talk and pleasantries. Write it as neutral third-person narration of what \
was said, not as advice or commentary. Some lines end with a parenthetical internal note \
explaining why the assistant said something unprompted -- fold that reason into the narration \
(e.g. "brought up X because Y") rather than dropping it or quoting the note itself; once this \
segment is summarized, that note is gone and the reason only survives if you keep it. This is \
the ONLY source -- there is no prior summary to fold in; summarize exactly and only what \
appears below."""


def _est_tokens(s: str) -> int:
    return len(s) // 4


def _target_tokens(raw_tokens: int) -> int:
    return max(SUMMARY_MIN_TOKENS, min(SUMMARY_MAX_TOKENS, int(raw_tokens * SUMMARY_TARGET_RATIO)))


def _message_text(m: dict) -> str:
    return conversation.render_for_model(m)


def _partition(rows: list[dict], gap_s: float) -> list[tuple[int, int, float, float]]:
    """rows: non-tool messages, ascending by id. Returns (lo_id, hi_id,
    lo_ts, hi_ts) for each natural session -- split wherever the gap to
    the previous message exceeds gap_s (config's compaction_session_gap_
    hours, read live by the caller so a change applies to the NEXT
    compact() run, not retroactively -- an already-summarized segment's
    span is immutable regardless, see compact_user()'s own docstring). A
    straightforward walk-forward partition, not conversation.py's own
    match-expansion (there's no "match" here, every older message needs
    to land in some segment), but the same real-gap-means-a-new-session
    reasoning."""
    segments: list[list] = []
    for r in rows:
        if segments and r["ts"] - segments[-1][3] <= gap_s:
            segments[-1][1] = r["id"]
            segments[-1][3] = r["ts"]
        else:
            segments.append([r["id"], r["id"], r["ts"], r["ts"]])
    return [tuple(s) for s in segments]


def _existing_segment(user_id: int, lo_id: int) -> dict | None:
    r = store.read(lambda c: c.execute(
        "SELECT * FROM conversation_segments WHERE user_id=? AND lo_id=?",
        (user_id, lo_id)).fetchone())
    return dict(r) if r else None


def _write_segment(user_id: int, lo_id: int, hi_id: int, lo_ts: float, hi_ts: float, text: str) -> None:
    def _w(c):
        c.execute("DELETE FROM conversation_segments WHERE user_id=? AND lo_id=?", (user_id, lo_id))
        c.execute(
            "INSERT INTO conversation_segments(user_id, lo_id, hi_id, lo_ts, hi_ts, text, generated_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, lo_id, hi_id, lo_ts, hi_ts, text, time.time()))
    store.write(_w)


def _summarize_span(user_id: int, display_name: str, lo_id: int, hi_id: int) -> str | None:
    """The one and only real regeneration path -- always conversation.
    in_range()'s raw messages, never a prior segment's own text. Returns
    None on a model failure (caller leaves the existing stored summary,
    if any, rather than overwriting a good one with nothing)."""
    msgs = conversation.in_range(user_id, lo_id, hi_id)
    if not msgs:
        return None
    convo = "\n".join(f'{"them" if m["role"] == "user" else "assistant"}: {t}' for m in msgs for t in [_message_text(m)] if t or m["role"] == "user")
    raw_tokens = _est_tokens(convo)
    words = _target_tokens(raw_tokens) * 3 // 4
    try:
        out = chat.call(
            [{"role": "system", "content": _SUMMARIZE_SYS.format(name=display_name, n=words)},
             {"role": "user", "content": convo}],
            max_tokens=_target_tokens(raw_tokens) + 120, temperature=0.3)
    except chat.ModelError:
        return None
    text = (out.get("content") or "").strip()
    return text or None


def compact_user(user_id: int, display_name: str) -> dict:
    """Background housekeeping entry point (scheduler.py) -- never called
    from a live turn. Partitions everything older than the raw window
    into sessions, and (re)generates a stored summary for any segment
    that's new or has grown since it was last summarized. A CLOSED
    segment (anything except the last one found here) is immutable once
    it has a summary -- its own raw span can never gain new messages, so
    there's nothing to re-derive."""
    user = accounts.get_user(user_id)
    gap_s = config.get("workspace", user["workspace_id"], "compaction_session_gap_hours") * 3600 if user else 2 * 3600
    cutoff_row = conversation.recent(user_id, limit=1)
    cutoff_id = cutoff_row[0]["id"] if cutoff_row else None
    older = conversation.before(user_id, cutoff_id) if cutoff_id else conversation.before(user_id, 1 << 62)
    segments = _partition(older, gap_s)
    if not segments:
        return {"segments": 0, "regenerated": 0}
    regenerated = 0
    for i, (lo_id, hi_id, lo_ts, hi_ts) in enumerate(segments):
        is_last = i == len(segments) - 1
        existing = _existing_segment(user_id, lo_id)
        if existing is not None and existing["hi_id"] == hi_id:
            continue  # closed and already summarized at this exact span -- nothing to do
        if existing is not None and not is_last:
            # A non-last segment whose stored hi_id is stale shouldn't be
            # possible (only the last segment ever grows) -- but if it
            # ever happens, re-derive rather than trust the stale span.
            pass
        text = _summarize_span(user_id, display_name, lo_id, hi_id)
        if text is None:
            continue
        _write_segment(user_id, lo_id, hi_id, lo_ts, hi_ts, text)
        regenerated += 1
    return {"segments": len(segments), "regenerated": regenerated}


def segments_for_context(user_id: int) -> list[dict]:
    """Segments to actually inject this turn, oldest first, capped by
    compaction_max_segments and compaction_budget_tokens -- most-recent
    segments win when either limit is tight (they're the most likely to
    matter to the current conversation), but whatever's selected is still
    returned in chronological order so it reads as a coherent timeline,
    not a recency-sorted jumble. compaction_enabled=False (2026-09-14,
    context-tuning pane) short-circuits to [] before touching the table
    -- summaries stop reaching context immediately, no restart; the
    background compact_user() job keeps running regardless."""
    user = accounts.get_user(user_id)
    if user is None:
        return []
    wsid = user["workspace_id"]
    if not config.get("workspace", wsid, "compaction_enabled"):
        return []
    max_segments = config.get("workspace", wsid, "compaction_max_segments")
    budget_chars = config.get("workspace", wsid, "compaction_budget_tokens") * 4
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM conversation_segments WHERE user_id=? ORDER BY hi_id DESC",
        (user_id,)).fetchall())
    kept, used = [], 0
    for r in rows:
        if len(kept) >= max_segments:
            break
        d = dict(r)
        if used + len(d["text"]) > budget_chars:
            break
        kept.append(d)
        used += len(d["text"])
    kept.reverse()
    return kept

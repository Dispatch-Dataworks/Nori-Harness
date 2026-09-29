# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Per-user conversation storage — the only module that runs raw SQL against
`messages` (see store.py's narrow-abstraction rule). Every function here
takes a user_id and every query is scoped by it; nothing in this module
will ever return one user's messages to a call scoped for another.
"""
from __future__ import annotations

import json
import time

import own_output
import store
import usertime

# How much history rides along on every turn. No rolling-summary layer yet
# (a deliberate scope cut for this phase) -- just the most recent N messages,
# same as a sibling application started with before it grew a summary layer.
DEFAULT_WINDOW = 30

# Hide historical emotion housekeeping lines as well as new ones -- and,
# same reasoning, message_user (2026-09-14, operator's own ask): its own
# text already lands in his chat a moment before "used message_user"
# would, so the line duplicates what he just read rather than telling him
# anything new. Display suppression only, chat transcript only -- see
# server.py's history_page, which reads `messages` directly and applies
# no such filter, on purpose: it exists specifically to show everything,
# and every other tool call still shows here regardless (deliberate,
# operator's own explicit "his only window into what they did during a
# peer turn" -- this is one narrowly-named exception, not a policy
# change). Apply before LIMIT so hidden rows cannot crowd visible
# messages out of a page.
VISIBLE_TOOLS_FILTER = (" AND NOT (kind='tool' AND trim(content) IN "
                       "('used set_emotion', 'used message_user', "
                       "'used generate_image_selfie', 'used imagine_image'))")

# kind values a self-initiated message can carry -- working-memory metadata
# (meta.reason) is only ever rendered for these (render_for_model, below).
# A real reply ('chat') never needs one: the operator's own message right
# above it already IS the reason.
_SELF_INITIATED_KINDS = ("proactive", "peer_proactive", "job_proactive")


def add_message(user_id: int, role: str, content: str, *, emotion: str | None = None,
                kind: str = "chat", meta: dict | None = None) -> int:
    """emotion is the state Nori was in when an assistant message was sent
    (None for user messages -- there's nothing to record). Recorded at
    send time, not recomputed later, so scrolling back shows how she
    actually was then, not a decayed-to-neutral reinterpretation of it.
    kind='proactive' marks a message she sent unprompted (scheduler.py),
    vs the default 'chat' for an actual reply.

    meta (2026-09-13, operator's own "working-memory metadata" ask) is a
    small dict -- in practice just {"reason": "..."}, sometimes plus
    {"peer": name} -- explaining why a self-initiated message happened.
    Stored as its own column, never folded into `content`: the chat page
    and any export render `content` verbatim, so this stays invisible to
    him exactly as asked. See render_for_model() for the one place it
    actually reaches the model."""
    def _w(c):
        cur = c.execute(
            "INSERT INTO messages(user_id, ts, role, content, emotion, kind, meta) VALUES (?,?,?,?,?,?,?)",
            (user_id, time.time(), role, content, emotion, kind,
             json.dumps(meta) if meta else None))
        return cur.lastrowid
    return store.write(_w)


def render_for_model(m: dict) -> str:
    """What the model should see for this message -- for a self-initiated
    one with a recorded reason, that's the stored content plus a trailing
    aside explaining why she said it, so a LATER turn asked "why did you
    say that" has the real answer instead of a gap it would otherwise fill
    with a fluent, wrong reconstruction (the operator's own real example: a
    sibling application's own assistant asked if he was awake because Nori
    had said so over the peer channel, then couldn't account for her own
    question a turn later and invented a plausible-sounding reason instead).

    Deliberately NOT touching `content` itself or anything the chat page/
    export renders -- this is only ever called building a message list for
    the model (context.build_messages, compaction._message_text), never
    for display. This is also the only place the aside is added, so it's
    naturally absent once a message has been folded into a compacted
    segment (compaction.py re-derives from raw rows via this same
    function, then the segment's own prose -- see its _SUMMARIZE_SYS
    -- is what carries the reason forward from then on, not this)."""
    if m.get("kind") == "image":
        return _image_message_text(m)
    # Her own words go through own_output.scrub (a stored artefact she once produced must not become a template for the next reply); the app's notes are
    # added AFTER, so they are never mistaken for artefacts. May return "" (the message was only an artefact): callers skip empty assistant lines.
    c = own_output.scrub(m["content"], m.get("role", "assistant"))
    if m.get("kind") not in _SELF_INITIATED_KINDS or not m.get("meta") or not c:
        return c
    try:
        meta = json.loads(m["meta"])
    except (ValueError, TypeError):
        return c
    reason = meta.get("reason")
    if not reason:
        return c
    return (f"{c}\n(internal note to self -- not part of what you actually said, and this note "
           f"itself never appears anywhere they can see: the real reason you said this was "
           f"{reason}. If they ever ask why, that's the true, honest answer -- give it plainly "
           f"rather than guessing or deflecting.)")


def _image_message_text(m: dict) -> str:
    """What an image message actually says, for feeding to the model
    (2026-09-15, ported from a sibling application's identical branch in its own
    _message_text -- Nori never had user-sent chat photos before, so
    this case never existed here). A photo's vision description
    (meta.description, set once at upload time by
    imagegen.describe_chat_photo) is reused here every time this message
    is rendered, in the window or later in a compacted summary -- one
    vision call, ever, not one per render. The turn a photo is actually
    introduced in departs from this (it gets the real image directly via
    an extra_message, not this text -- see
    imagegen.chat_photo_extra_user_multimodal); every render after that
    comes through here. No description at all (vision off at upload, or
    the call failed) falls back to the plain "sent an image" annotation,
    same honest posture as the fallback framing on the introducing turn
    itself (imagegen.chat_photo_extra_user)."""
    c = m["content"]
    if m["role"] != "user":
        # A marked app note, never a bracketed form she could type as her own reply (she did, twice, in real history, when this was "[you sent him a photo] with: ...").
        caption = own_output.scrub(c, "assistant")
        return own_output.app_note("you sent the user a picture you generated" + (f', captioned "{caption}"' if caption.strip() else ""))
    meta = {}
    if m.get("meta"):
        try:
            meta = json.loads(m["meta"])
        except (ValueError, TypeError):
            pass
    desc = meta.get("description")
    if desc:
        return f"[the user sent a photo: {desc}]" + (f' -- captioned "{c}"' if c.strip() else "")
    return "[the user sent an image" + (f': {c}' if c.strip() else "") + "]"


# ── per-turn cost logging (2026-09-13, operator's own ask: "make cost
# answerable from data, by day and by what triggered the turn," asked
# three times in one day and estimated every time) ─────────────────────
def cost_meta(usage: dict) -> dict:
    """The cost_usd/cost_unavailable/prompt_tokens/completion_tokens fields
    every real, cost-relevant message gets in its own meta -- merged
    alongside working-memory metadata's own reason field where one exists
    (same record, same mechanism, not a second one), or on its own for an
    ordinary reply (kind='chat', which had no meta at all before this).
    cost_usd is chat.run()'s own real, summed dollar total for the whole
    turn, or None if any round in it didn't report one (a direct-provider
    call -- see chat.call()'s own docstring); cost_unavailable makes that
    explicit rather than leaving a bare null to be misread as "free," which
    doesn't happen with a real per-token rate. Token counts are always
    real regardless of provider, so those are still worth keeping even
    when the dollar figure isn't available."""
    cost = usage.get("cost")
    return {"cost_usd": cost, "cost_unavailable": cost is None,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0)}


def attach_cost_since(user_id: int, after_id: int, usage: dict) -> None:
    """Merges cost_meta(usage) into every kind='peer_proactive' row this
    user gained after after_id -- for a peer-motivated turn, where the row
    that matters (message_user's own write, if she calls it) happens MID-
    turn, before chat.run() has finished and the turn's total cost is
    known. Safe to treat "everything after after_id" as unambiguously
    THIS turn's own output: turns.py's own lock guarantees only one turn
    ever runs for a given account at a time, same snapshot-before/diff-
    after pattern its own sweep already uses."""
    meta = cost_meta(usage)
    def _w(c):
        rows = c.execute(
            "SELECT id, meta FROM messages WHERE user_id=? AND id>? AND kind='peer_proactive'",
            (user_id, after_id)).fetchall()
        for r in rows:
            existing = {}
            if r["meta"]:
                try:
                    existing = json.loads(r["meta"])
                except (ValueError, TypeError):
                    existing = {}
            existing.update(meta)
            c.execute("UPDATE messages SET meta=? WHERE id=?", (json.dumps(existing), r["id"]))
    store.write(_w)


def cost_summary(user_id: int, days: int = 7) -> dict:
    """Real spend, today and over the last `days` days, broken down by
    kind -- "what is [the peer channel/proactive pings/ordinary chat]
    actually costing me," answerable from data rather than estimated
    (operator's own ask, after being asked to estimate it three times in
    one day). Aggregated in Python over a bounded row fetch, not a SQL
    json_extract query -- this household's own message volume is small
    enough that this is simpler and more portable than depending on a
    particular SQLite build's JSON1 support. A bucket's own "cost" is
    real money only for the rows inside it that reported one; its own
    "unavailable" count says how many didn't, so the number is never
    quietly short -- see cost_meta()'s own docstring for why a real
    total is never partially summed and passed off as complete."""
    since = time.time() - days * 86400
    today_since = usertime.day_start(user_id)   # 2026-09-25, the operator: "fix budgets to all use timezones" -- was time.time() % 86400, always UTC-midnight
    rows = store.read(lambda c: c.execute(
        "SELECT ts, kind, meta FROM messages WHERE user_id=? AND role='assistant' "
        "AND ts>=? AND meta IS NOT NULL", (user_id, since)).fetchall())
    period = {"cost": 0.0, "unavailable": 0, "n": 0, "by_kind": {}}
    today = {"cost": 0.0, "unavailable": 0, "n": 0}
    for r in rows:
        try:
            meta = json.loads(r["meta"]) if r["meta"] else {}
        except (ValueError, TypeError):
            continue
        if "cost_usd" not in meta:
            continue
        bucket = period["by_kind"].setdefault(r["kind"], {"cost": 0.0, "unavailable": 0, "n": 0})
        for b in (period, bucket):
            b["n"] += 1
            if meta.get("cost_unavailable"):
                b["unavailable"] += 1
            else:
                b["cost"] += meta.get("cost_usd") or 0.0
        if r["ts"] >= today_since:
            today["n"] += 1
            if meta.get("cost_unavailable"):
                today["unavailable"] += 1
            else:
                today["cost"] += meta.get("cost_usd") or 0.0
    return {"days": days, "period": period, "today": today}


def last_message_ts(user_id: int) -> float | None:
    r = store.read(lambda c: c.execute(
        "SELECT max(ts) AS ts FROM messages WHERE user_id=?", (user_id,)).fetchone())
    return r["ts"]


def recent(user_id: int, limit: int = DEFAULT_WINDOW, *, include_tool: bool = False) -> list[dict]:
    """include_tool=True is for chat DISPLAY only. Defaulting False means
    every existing caller -- context.build_messages (what the model sees)
    and memory.py's reflection window -- excludes kind='tool' rows with no
    code change of their own; only chat_page's own render, which passes
    include_tool explicitly, ever shows her own tool-call lines. She never
    sees them either way, same principle as the reader/actor split: what's
    shown to the human isn't necessarily what reaches the model."""
    kind_filter = VISIBLE_TOOLS_FILTER if include_tool else " AND kind!='tool'"
    rows = store.read(lambda c: c.execute(
        f"SELECT * FROM messages WHERE user_id=?{kind_filter} ORDER BY id DESC LIMIT ?",
        (user_id, limit)).fetchall())
    return [dict(r) for r in reversed(rows)]


def count(user_id: int) -> int:
    r = store.read(lambda c: c.execute(
        "SELECT count(*) AS n FROM messages WHERE user_id=?", (user_id,)).fetchone())
    return r["n"]


def max_id(user_id: int) -> int:
    """Highest message id this user has, 0 if none yet -- turns.py's own
    snapshot-before/after-a-turn bookkeeping (see its module docstring)."""
    r = store.read(lambda c: c.execute(
        "SELECT COALESCE(MAX(id),0) AS n FROM messages WHERE user_id=?", (user_id,)).fetchone())
    return r["n"]


def min_id(user_id: int) -> int:
    """Lowest message id this user has, 0 if none yet -- server.py's
    history_page uses this to know whether an "older" page link would
    actually return anything, rather than always showing one."""
    r = store.read(lambda c: c.execute(
        "SELECT COALESCE(MIN(id),0) AS n FROM messages WHERE user_id=?", (user_id,)).fetchone())
    return r["n"]


def latest_user_message_after(user_id: int, after_id: int) -> dict | None:
    """The newest user-authored row past `after_id`, if any -- what turns.py
    sweeps for after finishing a turn, to catch a message that arrived
    while it was running rather than dropping or racing it."""
    r = store.read(lambda c: c.execute(
        "SELECT * FROM messages WHERE user_id=? AND id>? AND role='user' "
        "ORDER BY id DESC LIMIT 1", (user_id, after_id)).fetchone())
    return dict(r) if r else None


def since(user_id: int, after_id: int, limit: int = 200, *, include_tool: bool = False) -> list[dict]:
    """Every message past `after_id`, oldest first -- GET /poll's own feed,
    so a second tab (or a proactive ping that landed with nobody watching)
    shows up without a reload. include_tool -- see recent()'s docstring;
    same display-only meaning here."""
    kind_filter = VISIBLE_TOOLS_FILTER if include_tool else " AND kind!='tool'"
    rows = store.read(lambda c: c.execute(
        f"SELECT * FROM messages WHERE user_id=? AND id>?{kind_filter} ORDER BY id ASC LIMIT ?",
        (user_id, after_id, limit)).fetchall())
    return [dict(r) for r in rows]


def get_own(user_id: int, msg_id: int) -> dict | None:
    """A single row, but ONLY if it belongs to this user -- POST /retry's
    ownership check. A wrong or someone-else's id reads as "no such
    message," never leaking whether it exists for another user."""
    r = store.read(lambda c: c.execute(
        "SELECT * FROM messages WHERE user_id=? AND id=?", (user_id, msg_id)).fetchone())
    return dict(r) if r else None


def has_reply_after(user_id: int, msg_id: int) -> bool:
    """True if anything (from either side) already exists past `msg_id` --
    /retry's own guard against re-answering a message a sweep (or a
    concurrent retry from another tab) already handled."""
    r = store.read(lambda c: c.execute(
        "SELECT 1 FROM messages WHERE user_id=? AND id>? LIMIT 1", (user_id, msg_id)).fetchone())
    return r is not None


def count_since_ts(user_id: int, ts: float) -> int:
    """How many messages (either role) have landed since `ts` -- memory.py's
    reflection cadence check ("N messages since last reflection")."""
    r = store.read(lambda c: c.execute(
        "SELECT count(*) AS n FROM messages WHERE user_id=? AND ts>?", (user_id, ts)).fetchone())
    return r["n"]


def before(user_id: int, before_id: int) -> list[dict]:
    """Every non-tool message strictly older than `before_id`, oldest
    first -- compaction.py's own view of "everything the raw window
    doesn't cover," to be partitioned into sessions and summarized.
    Unbounded on purpose (unlike recent()'s LIMIT): compaction runs in
    the background, not on a live turn, so there's no per-request cost
    to control here the way there is for a real chat.run() call."""
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM messages WHERE user_id=? AND id<? AND kind!='tool' ORDER BY id ASC",
        (user_id, before_id)).fetchall())
    return [dict(r) for r in rows]


def in_range(user_id: int, lo_id: int, hi_id: int) -> list[dict]:
    """Every non-tool message in [lo_id, hi_id] inclusive -- exactly the
    source compaction.py re-derives one segment's summary from. Never
    fed a prior summary alongside this; the raw span is the only input,
    on purpose (see conversation_segments' own schema comment)."""
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM messages WHERE user_id=? AND id>=? AND id<=? AND kind!='tool' ORDER BY id ASC",
        (user_id, lo_id, hi_id)).fetchall())
    return [dict(r) for r in rows]


def last_user_message_ts(user_id: int) -> float | None:
    """Distinct from last_message_ts (any role) -- specifically the last
    time THEY said something, which is what "reflect after a long gap"
    actually means: time since they were last around, not time since she
    last spoke (a run of her own proactive pings with no reply shouldn't
    look like recent activity for this purpose)."""
    r = store.read(lambda c: c.execute(
        "SELECT max(ts) AS ts FROM messages WHERE user_id=? AND role='user'", (user_id,)).fetchone())
    return r["ts"]


# ── full-history web view (2026-09-14, operator's own ask) -- a human
# reading everything, not the model's own bounded context. Paged, newest
# first by default, every kind included (this view is deliberately not
# curated the way chat_page's DEFAULT_WINDOW/include_tool split is -- the
# whole point is seeing what's normally hidden). Search here means "find
# where in the real timeline this happened," not "list matching lines" --
# see history_match_ids() below and server.py's own history_page.
HISTORY_PAGE_SIZE = 50


def history_page(user_id: int, *, before_id: int | None = None, after_id: int | None = None,
                 center_id: int | None = None, limit: int = HISTORY_PAGE_SIZE,
                 kind: str | None = None) -> list[dict]:
    """Always returns newest-first, regardless of which cursor drove the
    query -- server.py renders one consistent order regardless of how the
    page was reached. Exactly one of before_id/after_id/center_id should
    be set (center_id wins if more than one is, since that's the "jump to
    a search hit" path and takes priority over an ordinary page cursor);
    all three unset means "the latest page," the default landing view.

    center_id splits the limit roughly in half so the hit lands near the
    middle of the page, with real context on both sides -- the whole
    reason this exists instead of a plain filtered results list.

    kind (2026-09-15, the photo reel) narrows to one message kind (e.g.
    'image') without duplicating this cursor logic in a second function
    -- the reel reuses this exact paging mechanism, just filtered."""
    kind_clause, kind_params = (" AND kind=?", (kind,)) if kind is not None else ("", ())
    if center_id is not None:
        half = max(1, limit // 2)
        newer = store.read(lambda c: c.execute(
            f"SELECT * FROM messages WHERE user_id=? AND id>=?{kind_clause} ORDER BY id ASC LIMIT ?",
            (user_id, center_id, *kind_params, half)).fetchall())
        older = store.read(lambda c: c.execute(
            f"SELECT * FROM messages WHERE user_id=? AND id<?{kind_clause} ORDER BY id DESC LIMIT ?",
            (user_id, center_id, *kind_params, limit - half)).fetchall())
        rows = list(newer) + list(older)
        rows.sort(key=lambda r: r["id"], reverse=True)
        return [dict(r) for r in rows]
    if after_id is not None:
        rows = store.read(lambda c: c.execute(
            f"SELECT * FROM messages WHERE user_id=? AND id>?{kind_clause} ORDER BY id ASC LIMIT ?",
            (user_id, after_id, *kind_params, limit)).fetchall())
        rows = list(reversed(rows))
    else:
        clause, params = "", [user_id]
        if before_id is not None:
            clause = " AND id<?"
            params.append(before_id)
        rows = store.read(lambda c: c.execute(
            f"SELECT * FROM messages WHERE user_id=?{clause}{kind_clause} ORDER BY id DESC LIMIT ?",
            (*params, *kind_params, limit)).fetchall())
    return [dict(r) for r in rows]


def history_match_ids(user_id: int, *, query: str | None = None, hours_ago: float | None = None,
                      limit: int = 500) -> list[int]:
    """Bare matching ids, newest first -- NOT a results list to display
    (see module comment above): server.py uses this only to know where to
    jump to and to drive "next match"/"previous match" navigation once
    already viewing a page. Same LIKE-escaping as search(), deliberately
    not reusing search()'s own segmenting/clipping -- that machinery
    exists to fit a bounded token budget for the model; this just needs
    ids."""
    if not query and hours_ago is None:
        return []
    clauses, params = ["user_id=?", "kind!='tool'"], [user_id]
    if hours_ago is not None:
        clauses.append("ts>=?")
        params.append(time.time() - max(0.0, float(hours_ago)) * 3600)
    if query:
        clauses.append("content LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(query)}%")
    rows = store.read(lambda c: c.execute(
        f"SELECT id FROM messages WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
        (*params, limit)).fetchall())
    return [r["id"] for r in rows]


# ── history search (2026-09-12) -- complement to memory.py's typed recall,
# not a replacement for it. Real gap this closes: something discussed
# earlier today, never turned into a memory, and now outside the
# DEFAULT_WINDOW she actually sees -- "we talked about this" is true and
# she has no way to reach it. Plain LIKE, same posture as memory.py's own
# search (portable, genuinely enough at household scale) -- no FTS5.
_SEARCH_MAX_SEGMENTS = 8
_SEARCH_MAX_MATCHES = 40  # matches considered before segmenting/capping, not the final output size
_SEARCH_CONTENT_CAP = 400  # chars kept per message inside a segment
# Stop expanding a segment across a gap bigger than this -- crossing it
# means a different conversation, not more context on the same one. Same
# "a real gap is a session boundary" reasoning a sibling application/context.py's own
# _gap_label uses (there: 900s/15min, for a UI marker on a message that's
# already going to be shown); this is looser on purpose since the cost of
# under-including one borderline-relevant neighbour is much lower than the
# cost of silently dragging an unrelated earlier exchange into a segment
# just because nothing else happened to land between them in the log.
_SEGMENT_GAP_S = 1800


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clip(s: str) -> str:
    return s if len(s) <= _SEARCH_CONTENT_CAP else s[:_SEARCH_CONTENT_CAP] + "…"


def search(user_id: int, *, query: str | None = None, hours_ago: float | None = None,
          context_before: int = 2, context_after: int = 2,
          max_segments: int = 5) -> dict:
    """Real history search, not a memory lookup -- see memory.py's own
    module docstring on the split this mirrors: memory is a small,
    curated, on-demand slice of facts worth keeping; this is the
    complete, unfiltered log, searched only when something specific is
    being looked for. Requires at least one of query/hours_ago -- a call
    with neither is "show me everything," which is exactly the
    unbounded-cost dump this function exists to avoid.

    Returns SEGMENTS, not bare matching lines: each match is expanded to
    include context_before/context_after messages around it (a matching
    line without the exchange around it is usually useless), and
    overlapping or adjacent windows are merged into one segment rather
    than returned as duplicates or split arbitrarily. Segments are capped
    (max_segments, itself hard-capped at _SEARCH_MAX_SEGMENTS) and kept
    most-recent-first -- recency is the only ranking signal this has (no
    real relevance scoring over a LIKE search), which also happens to be
    exactly what "what did we talk about this morning" needs. Per-message
    content is clipped (_SEARCH_CONTENT_CAP) so one long message can't
    blow the whole budget -- same truncate-and-flag posture as
    ingest.py's PRESERVE_CAP, applied here since this is real content
    read back to her, not a triage summary."""
    if not query and hours_ago is None:
        return {"error": "give a query, a time window (hours_ago), or both -- "
                        "searching with neither would return your entire history"}
    max_segments = max(1, min(int(max_segments or 5), _SEARCH_MAX_SEGMENTS))

    clauses = ["user_id=?", "kind!='tool'"]
    params: list = [user_id]
    if hours_ago is not None:
        clauses.append("ts>=?")
        params.append(time.time() - max(0.0, float(hours_ago)) * 3600)
    only_window_hit = False
    if query:
        clauses.append("content LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(query)}%")
        # A keyword match inside the live visible window (the same
        # DEFAULT_WINDOW messages recent() already puts in front of her
        # every turn) is never informative -- she already has it in
        # context -- and it's actively misleading: found the hard way
        # (2026-09-12, a real production case) when a user asked "find
        # your dinner suggestion from earlier" and the model's own query
        # ("dinner suggestion") matched nothing but the CURRENT question
        # asking that, plus her own prior "couldn't find it" reply, both
        # sitting in the live window -- a real, older answer existed
        # (a genuine meal suggestion, worded differently, well outside
        # the window) but the self-referential match inside the window
        # returned first and satisfied the search, so nothing further was
        # ever tried. Excluding the live window from MATCH eligibility
        # closes that trap -- only applied to query search (hours_ago-only
        # browsing is deliberately allowed to include recent messages;
        # there's no self-reference risk there). Context expansion below
        # can still reach INTO this range once a genuine older match
        # triggers a segment -- this only blocks the window from being
        # the match itself.
        window_ids = [m["id"] for m in recent(user_id)]
        if window_ids:
            unrestricted_sql = (f"SELECT 1 FROM messages WHERE {' AND '.join(clauses)} LIMIT 1")
            any_hit_at_all = store.read(lambda c: c.execute(unrestricted_sql, params).fetchone()) is not None
            clauses.append("id<?")
            params.append(min(window_ids))
            if any_hit_at_all:
                restricted_sql = (f"SELECT 1 FROM messages WHERE {' AND '.join(clauses)} LIMIT 1")
                only_window_hit = store.read(
                    lambda c: c.execute(restricted_sql, params).fetchone()) is None
    sql = (f"SELECT id, ts FROM messages WHERE {' AND '.join(clauses)} "
          "ORDER BY id DESC LIMIT ?")
    params.append(_SEARCH_MAX_MATCHES)
    matches = [(r["id"], r["ts"]) for r in store.read(lambda c: c.execute(sql, params).fetchall())]
    if not matches:
        if only_window_hit:
            return {"segments": [], "match_count": 0,
                    "note": "that word only matched your CURRENT visible conversation, which you "
                            "already have in context -- this doesn't mean it never happened further "
                            "back; try a different or broader word for what you're looking for."}
        return {"segments": [], "match_count": 0}

    # Expand each match to a [lo, hi] id range by walking outward one
    # message at a time, stopping early at a real time gap (_SEGMENT_GAP_S)
    # rather than blindly taking the N nearest ids -- a household's
    # conversations are bursty, so "2 messages before" can otherwise reach
    # straight past a two-day silence into a completely unrelated,
    # long-past exchange that just happens to sit next to this one in the
    # log. Walked one row at a time (not a single LIMIT-N query) so each
    # step's gap can be checked against the row before it.
    def _expand(mid: int, mts: float) -> tuple[int, int, float, float]:
        lo, lo_ts = mid, mts
        hi, hi_ts = mid, mts
        cur_id, cur_ts = mid, mts
        for _ in range(context_before):
            row = store.read(lambda c: c.execute(
                "SELECT id, ts FROM messages WHERE user_id=? AND id<? AND kind!='tool' "
                "ORDER BY id DESC LIMIT 1", (user_id, cur_id)).fetchone())
            if row is None or cur_ts - row["ts"] > _SEGMENT_GAP_S:
                break
            lo, lo_ts = row["id"], row["ts"]
            cur_id, cur_ts = row["id"], row["ts"]
        cur_id, cur_ts = mid, mts
        for _ in range(context_after):
            row = store.read(lambda c: c.execute(
                "SELECT id, ts FROM messages WHERE user_id=? AND id>? AND kind!='tool' "
                "ORDER BY id ASC LIMIT 1", (user_id, cur_id)).fetchone())
            if row is None or row["ts"] - cur_ts > _SEGMENT_GAP_S:
                break
            hi, hi_ts = row["id"], row["ts"]
            cur_id, cur_ts = row["id"], row["ts"]
        return (lo, hi, lo_ts, hi_ts)

    match_ids = [mid for mid, _ in matches]
    ranges = sorted((_expand(mid, mts) for mid, mts in matches), key=lambda r: r[0])
    # Merge two ranges only when they're BOTH id-adjacent/overlapping AND
    # within _SEGMENT_GAP_S of each other in real time -- id-adjacency alone
    # isn't enough (see _expand's own docstring: two separately-expanded
    # clusters can still end up back-to-back in id space with nothing
    # logged between them, if the household simply went quiet for hours in
    # between, which is exactly the case merging must NOT paper over).
    merged: list[list] = []  # each: [lo, hi, lo_ts, hi_ts]
    for lo, hi, lo_ts, hi_ts in ranges:
        if merged and lo <= merged[-1][1] + 1 and lo_ts - merged[-1][3] <= _SEGMENT_GAP_S:
            merged[-1][1] = max(merged[-1][1], hi)
            merged[-1][3] = max(merged[-1][3], hi_ts)
        else:
            merged.append([lo, hi, lo_ts, hi_ts])

    # Most-recent segment first (see docstring on why recency is the
    # ranking signal), capped to max_segments -- match_count below still
    # reports the true total so a narrower query is visibly the fix, not
    # a guess.
    merged.sort(key=lambda r: r[1], reverse=True)
    kept = merged[:max_segments]

    segments = []
    for lo, hi, _lo_ts, _hi_ts in kept:
        rows = store.read(lambda c: c.execute(
            "SELECT * FROM messages WHERE user_id=? AND id>=? AND id<=? AND kind!='tool' "
            "ORDER BY id ASC", (user_id, lo, hi)).fetchall())
        if not rows:
            continue
        start_ts = rows[0]["ts"]
        segments.append({
            "when": usertime.fmt(user_id, start_ts, "%A %Y-%m-%d %H:%M"),
            "messages": [{"role": r["role"], "content": _clip(own_output.scrub(r["content"], r["role"]))}
                         for r in rows if own_output.scrub(r["content"], r["role"]) or r["role"] != "assistant"],
        })
    return {"segments": segments, "match_count": len(match_ids),
            "note": None if len(merged) <= max_segments else
                   f"{len(merged)} distinct segments matched; showing the {max_segments} most recent -- "
                   f"narrow the query or the time window for the rest"}


def _search_history_impl(session: dict, *, query: str | None = None, hours_ago: float | None = None,
                        max_segments: int = 5) -> dict:
    return search(session["user_id"], query=query, hours_ago=hours_ago, max_segments=max_segments)


def _register_tools() -> None:
    import tools  # local: same reasoning as memory.py/emotion.py -- keeps tools.py from needing to know this module exists

    tools.register(tools.Tool(
        "search_history",
        {"type": "function", "function": {
            "name": "search_history",
            "description": (
                "Search your FULL conversation history -- not a memory lookup. Reach for this "
                "specifically when the operator references something you discussed that isn't in "
                "your current context and isn't something recall() turns up either -- 'we talked "
                "about this earlier', something from a time you can no longer see. Typed memory "
                "(remember/recall) is what's been deliberately kept and is always cheap to check; "
                "this searches the complete, unfiltered log and costs more, so check memory first "
                "for anything that sounds like a durable fact. Returns real excerpts (with "
                "surrounding context, not bare matching lines), most recent first. Give a query, a "
                "time window, or both -- at least one is required."),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string",
                         "description": "Free-text substring search over what was actually said. "
                                       "Omit to just browse a time window."},
                "hours_ago": {"type": "number",
                             "description": "Look back this many hours (e.g. 12 for 'this morning' "
                                            "if it's afternoon now). Omit to search all history."},
                "max_segments": {"type": "integer", "default": 5,
                                 "description": f"How many distinct exchanges to return, most recent "
                                               f"first (hard cap {_SEARCH_MAX_SEGMENTS})."},
            }}}},
        _search_history_impl, min_role="member", data_scope="self", risk_tier="A"))


_register_tools()

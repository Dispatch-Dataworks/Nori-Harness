# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Typed memory -- the only module that runs raw SQL against `memory`/
`memory_events` (see store.py's narrow-abstraction rule). This is the
actual fix for the context-bloat problem a sibling application hit: instead of one
flat facts list injected into every call, memory carries a `type` from a
fixed taxonomy, and callers pull just the slice relevant to what they're
doing -- a small always-on identity/preference slice (context_block) plus
an on-demand recall() tool for everything else, rather than the whole
store riding along on every turn.

Search is a plain LIKE, not SQLite's FTS5 extension -- deliberate: it's
both portable (see store.py's migration-posture note) and genuinely enough
at household scale. Tag filtering happens in Python after fetching by
type/user, not via SQL-level JSON functions, for the same reason.

Topic-triggered activation (2026-09-17) -- instead of relying on her to
remember to call recall() herself, an incoming message's own topics are
matched against stored memory and the strong matches are injected
automatically, before she decides what to say or do (topic_activation_line,
registered with precheck.py the same way pins_precheck_line already is)
and again, unconditionally for the safety/constraint tier, right before a
consequential tool call's result is folded back into the turn
(preaction_check, called from chat.py's tool loop). The matching itself
(tokenize/stem/score, cosine similarity) lives in topic_match.py, kept
free of any store/network dependency; this module owns the DB and the
OpenRouter embedding call, same narrow-abstraction split as everywhere
else here. See nori/docs/memory.md for the measured recall numbers this
design is based on, and why keyword matching alone isn't trusted for the
safety tier.
"""
from __future__ import annotations

import json
import os
import time

import accounts
import config
import store
import topic_match

# Fixed, code-level taxonomy -- the model can add VALUES (a new memory of an
# existing type) but not new TYPES; that's a deploy, on purpose, so this
# can't sprawl the way an unconstrained tag system would. Trim/extend this
# list deliberately, not by accretion.
TYPES = ("identity", "routine", "people", "email", "calendar",
         "household", "meals", "task", "preference")

# One-line disambiguation per type, for the reflection prompt (_REFLECT_SYS
# below) -- the thing that actually needs spelling out isn't "what does
# 'household' mean" in the abstract, it's "household/meals here means a
# durable fact about the household, NOT the live inventory/meal-plan data
# household.py/meals.py already own in their own tables." Getting that
# boundary wrong wouldn't just misfile a fact, it'd duplicate a system
# that already exists.
TYPE_HELP = {
    "identity": "who they are -- name, role in the household, fixed facts about them",
    "routine": "recurring patterns in how they live -- schedule, habits, regular commitments",
    "people": "other people in their life -- family, friends, contacts, and durable facts about them",
    "email": "durable facts about how they handle email/correspondence (not a specific email)",
    "calendar": "durable facts about their calendar/scheduling habits (not a specific event)",
    "household": ("durable facts ABOUT the household as a concept -- e.g. 'recycling is collected "
                 "Tuesdays'. NOT a live inventory item (there's a separate, dedicated inventory "
                 "system for that already -- 'we're low on milk' does not belong here)"),
    "meals": ("durable facts about food preferences/habits -- e.g. 'vegetarian on weekdays'. NOT a "
             "specific day's planned meal (there's a separate, dedicated meal-plan system for that "
             "already -- 'tacos on Tuesday' does not belong here)"),
    "task": ("a durable fact about how they handle tasks/commitments in general -- e.g. 'he tends to "
            "forget dentist appointments unless reminded twice'. NOT an actual task with a due date "
            "to track and put on his board (there's a separate, dedicated task/board system for that "
            "already, task_add/task_list/etc -- 'call the dentist Tuesday' does not belong here)"),
    "preference": "a stated like/dislike/preference that should shape how she talks to or helps them",
}

# The small, always-loaded slice -- everything else is on-demand via
# recall(). Capped hard, same discipline as a sibling application's memory budget.
# Pinned rows are NOT part of this slice (see PINNED_CONTEXT_BUDGET_CHARS
# below and precheck.py) -- they used to be, folded in here alongside
# identity/preference, but that put them in the same standing, position-0
# system-prompt block that today's emotion-injection work found the model
# discounts. A pin is the explicit "this always matters" signal; it gets
# the proximate, end-of-context treatment instead, not buried mid-prompt.
ALWAYS_LOAD_TYPES = ("identity", "preference")
# memory_max_tokens / memory_pinned_max_tokens (2026-09-14, context-tuning
# pane) -- moved to config.py, workspace-scoped, read live below instead
# of cached as module constants at import time. Same defaults these used
# to be as env vars (1500/600 chars ~= 375/150 tokens).

# Deliberately smaller than memory_max_tokens above -- pins are meant to
# be a few deliberately-chosen facts, not a bulk store; a generous budget
# here would defeat that by inviting a dumping ground charged on every
# single real turn (this rides in precheck.py's block, not the once-per-
# system-prompt slice). Configurable per operator, same as the general one.

MAX_TAGS = 6
MAX_TAG_LEN = 32

# Reflection cadence -- operator policy knobs (env constants, like
# NORI_TOOL_RATE_LIMIT), not per-user settings. Same two conditions as
# a sibling application's own reflection trigger, same defaults: N messages since the
# last reflection pass, OR a long gap since they were last around with at
# least one unreflected message waiting.
REFLECTION_EVERY_MSGS = int(os.environ.get("NORI_REFLECTION_EVERY_MSGS", "25"))
REFLECTION_GAP_HOURS = float(os.environ.get("NORI_REFLECTION_GAP_HOURS", "18"))

# Topic-triggered activation knobs -- env constants, same posture as the
# reflection ones above (operator policy, not per-user settings). 3.0
# matches a single tag hit exactly (TAG_WEIGHT in topic_match.py) -- an
# exact named-entity/category tag is "strong" enough alone; two bare
# content hits (2.0) still falls short, matching the operator's own "broad category
# alone: weaker" framing. Chosen from the real recall measurement, not a
# round-number guess -- see topic_match.py's own module docstring.
TOPIC_KEYWORD_THRESHOLD = float(os.environ.get("NORI_TOPIC_KEYWORD_THRESHOLD", "3.0"))
# Cosine similarity floor for the safety-tier semantic pass. Conservative
# on purpose -- a false positive here is a harmless extra line in context;
# a false negative is the exact failure mode this pass exists to catch.
TOPIC_SEMANTIC_THRESHOLD = float(os.environ.get("NORI_TOPIC_SEMANTIC_THRESHOLD", "0.55"))
TOPIC_INJECT_LIMIT = int(os.environ.get("NORI_TOPIC_INJECT_LIMIT", "5"))
# Char budget for what topic_activation_line/preaction_check inject --
# same reasoning as PINNED's own budget in pins_precheck_line: this rides
# on every real turn (or every consequential tool call), so an unbounded
# budget would be a real recurring cost, not a one-time one.
TOPIC_INJECT_CHAR_BUDGET = int(os.environ.get("NORI_TOPIC_INJECT_CHAR_BUDGET", "900"))
# OpenRouter's own embeddings endpoint (chat.openrouter_embed) -- reuses
# OPENROUTER_API_KEY, no new vendor/credential. text-embedding-3-small:
# small, cheap, well-covered dimensionality for short fact-length text;
# operator-overridable like every other model slug in this app.
EMBEDDING_MODEL = os.environ.get("NORI_EMBEDDING_MODEL", "openai/text-embedding-3-small")


# ── pluggable backend (2026-09-25) ───────────────────────────────────────
# "Point an existing mechanism at an external store" (the operator's own
# framing) means the READ/WRITE surface below has to be a real seam, not
# a rewrite -- every method here is the CURRENT local behavior, moved
# verbatim into one place, not redesigned. Local stays the reference
# implementation and the contract: a future backend (Nodrya or otherwise)
# implements the same methods; nothing here was pre-bent around a service
# this codebase hasn't seen yet. Every caller below (the tool
# implementations, context_block, pins_precheck_line, topic_activation_line,
# preaction_check, reflect) goes through _backend_for(), never raw SQL --
# this class is now the one place (alongside _log's own memory_events
# writes, which are Nori-side audit provenance, not backend data, and stay
# here regardless of backend) that touches the `memory` table, keeping
# store.py's narrow-abstraction rule intact.
class LocalMemoryBackend:
    """The only backend that exists today. Every method's body is the
    exact SQL/logic that used to live directly in memory.py's own
    functions -- see this class's own history (git blame) against the
    pre-seam version for a literal diff, not just a description."""

    def write(self, *, user_id: int, workspace_id: int, type_: str, value: str,
             tags: list | None, safety: bool, source: str = "tool") -> int:
        now = time.time()
        tags_json = json.dumps(_clean_tags(tags))
        return store.write(lambda c: c.execute(
            "INSERT INTO memory(user_id, workspace_id, scope, type, value, tags, source, "
            "created_ts, updated_ts, safety_tier) VALUES (?,?,'user',?,?,?,?, ?, ?, ?)",
            (user_id, workspace_id, type_, value, tags_json, source, now, now,
             1 if safety else 0)
        ).lastrowid)

    def update(self, *, user_id: int, memory_id: int, value: str | None,
              tags: list | None, safety: bool | None) -> dict | None:
        # Raw row, deliberately not self.get() -- tags/safety_tier are needed
        # in their raw stored form (JSON string / 0-or-1) as fallback values
        # for the UPDATE below, not the decoded (list/bool) form get() returns.
        row = store.read(lambda c: c.execute(
            "SELECT * FROM memory WHERE id=? AND user_id=?", (memory_id, user_id)).fetchone())
        if row is None:
            return None
        new_value = (value or "").strip() or row["value"]
        new_tags = json.dumps(_clean_tags(tags)) if tags is not None else row["tags"]
        new_safety = (1 if safety else 0) if safety is not None else row["safety_tier"]
        now = time.time()
        store.write(lambda c: c.execute(
            "UPDATE memory SET value=?, tags=?, updated_ts=?, safety_tier=? WHERE id=? AND user_id=?",
            (new_value, new_tags, now, new_safety, memory_id, user_id)))
        return {"type": row["type"], "value": new_value}

    def set_pinned(self, *, user_id: int, memory_id: int, pinned: bool) -> dict | None:
        row = self.get(user_id=user_id, memory_id=memory_id)
        if row is None:
            return None
        store.write(lambda c: c.execute(
            "UPDATE memory SET pinned=? WHERE id=? AND user_id=?",
            (1 if pinned else 0, memory_id, user_id)))
        return {"type": row["type"]}

    def set_safety_tier(self, *, user_id: int, memory_id: int, safety: bool) -> dict | None:
        row = self.get(user_id=user_id, memory_id=memory_id)
        if row is None:
            return None
        store.write(lambda c: c.execute(
            "UPDATE memory SET safety_tier=? WHERE id=? AND user_id=?",
            (1 if safety else 0, memory_id, user_id)))
        return {"type": row["type"]}

    def get(self, *, user_id: int, memory_id: int) -> dict | None:
        row = store.read(lambda c: c.execute(
            "SELECT * FROM memory WHERE id=? AND user_id=?", (memory_id, user_id)).fetchone())
        return _row_out(row) if row is not None else None

    def delete(self, *, user_id: int, memory_id: int) -> dict | None:
        row = self.get(user_id=user_id, memory_id=memory_id)
        if row is None:
            return None
        store.write(lambda c: c.execute(
            "DELETE FROM memory WHERE id=? AND user_id=?", (memory_id, user_id)))
        return {"type": row["type"], "value": row["value"]}

    def recall(self, *, user_id: int, types: list | None, tags: list | None,
              query: str | None, limit: int) -> list[dict]:
        clauses = ["user_id=?"]
        params: list = [user_id]
        if types:
            clauses.append(f"type IN ({','.join('?' * len(types))})")
            params.extend(types)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("value LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        sql = (f"SELECT * FROM memory WHERE {' AND '.join(clauses)} "
              "ORDER BY pinned DESC, updated_ts DESC LIMIT ?")
        params.append(limit)
        rows = store.read(lambda c: c.execute(sql, params).fetchall())

        tag_filter = set(_clean_tags(tags)) if tags else None
        out = []
        for r in rows:
            d = _row_out(r)
            if tag_filter and not (tag_filter & set(d["tags"])):
                continue
            out.append(d)

        if out:
            now = time.time()
            ids = [d["id"] for d in out]
            store.write(lambda c: c.executemany(
                "UPDATE memory SET last_used_ts=? WHERE id=?", [(now, i) for i in ids]))
        return out

    def context_slice(self, *, user_id: int, types: tuple) -> list[dict]:
        rows = store.read(lambda c: c.execute(
            f"SELECT * FROM memory WHERE user_id=? AND type IN ({','.join('?' * len(types))}) "
            "ORDER BY updated_ts DESC", (user_id, *types)).fetchall())
        return [_row_out(r) for r in rows]

    def pinned_rows(self, *, user_id: int) -> list[dict]:
        rows = store.read(lambda c: c.execute(
            "SELECT * FROM memory WHERE user_id=? AND pinned=1 ORDER BY updated_ts DESC",
            (user_id,)).fetchall())
        return [_row_out(r) for r in rows]

    def all_rows(self, *, user_id: int) -> list[dict]:
        rows = store.read(lambda c: c.execute(
            "SELECT * FROM memory WHERE user_id=? ORDER BY type, updated_ts DESC", (user_id,)).fetchall())
        return [_row_out(r) for r in rows]

    def safety_rows(self, *, user_id: int) -> list[dict]:
        rows = store.read(lambda c: c.execute(
            "SELECT * FROM memory WHERE user_id=? AND safety_tier=1 ORDER BY updated_ts DESC",
            (user_id,)).fetchall())
        return [_row_out(r) for r in rows]

    def topic_match(self, *, user_id: int, topics: list, threshold: float, limit: int) -> list[dict]:
        rows = self.all_rows(user_id=user_id)
        return topic_match.rank_keyword(topics, rows, threshold=threshold, limit=limit)

    def _embedding_for(self, row: dict) -> list[float] | None:
        """One safety-tier memory's cached embedding, computed and persisted
        on first use, recomputed if EMBEDDING_MODEL has changed since (a
        stale-model cache hit would silently compare vectors from two
        different embedding spaces, which is worse than recomputing).
        Returns None -- never raises -- on any provider failure; callers
        must treat that as "couldn't check this one," not a similarity of
        zero."""
        import chat  # local: same reasoning as everywhere else here -- avoids a top-level cycle
        if row.get("embedding_json") and row.get("embedding_model") == EMBEDDING_MODEL:
            try:
                return json.loads(row["embedding_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        res = chat.openrouter_embed([row["value"]], model=EMBEDDING_MODEL)
        if not res.get("ok"):
            print(f"memory.LocalMemoryBackend._embedding_for: embedding call failed for memory "
                 f"{row['id']}: {res.get('reason')}", flush=True)
            return None
        vec = res["vectors"][0]
        store.write(lambda c: c.execute(
            "UPDATE memory SET embedding_json=?, embedding_model=? WHERE id=?",
            (json.dumps(vec), EMBEDDING_MODEL, row["id"])))
        return vec

    def semantic_match(self, *, user_id: int, query_text: str, limit: int) -> tuple[list[dict], bool]:
        """Unconditional semantic pass over this user's safety/constraint
        tier -- never gated on keyword confidence (callers decide their own
        confidence posture; see topic_activation_line/preaction_check).
        Returns (matches, degraded); degraded=True means the embedding
        provider failed and matches may be incomplete or empty for a
        reason OTHER than "genuinely nothing relevant" -- callers must
        surface that distinction, never let it look like a clean all-clear."""
        import chat
        rows = self.safety_rows(user_id=user_id)
        if not rows:
            return [], False
        q = chat.openrouter_embed([query_text], model=EMBEDDING_MODEL)
        if not q.get("ok"):
            print(f"memory.LocalMemoryBackend.semantic_match: query embedding failed: "
                 f"{q.get('reason')} -- safety tier check degraded to keyword-only this turn", flush=True)
            return [], True
        qvec = q["vectors"][0]
        scored, any_failed = [], False
        for r in rows:
            vec = self._embedding_for(r)
            if vec is None:
                any_failed = True
                continue
            sim = topic_match.cosine(qvec, vec)
            if sim >= TOPIC_SEMANTIC_THRESHOLD:
                scored.append((sim, r))
        scored.sort(key=lambda t: -t[0])
        matches = [{"id": r["id"], "type": r["type"], "value": r["value"], "safety_tier": True,
                   "score": sim} for sim, r in scored[:limit]]
        return matches, any_failed


class NodryaBackendError(Exception):
    """A Nodrya MCP call failed, or returned something this backend can't
    use. Raised, never swallowed: write/update/delete either fully
    succeed (Nodrya AND the local cache both land) or the caller sees a
    clear failure -- tools.py's own dispatch (a broad except around every
    tool call) turns this into a safe error surface for the model, the
    same as any other tool-implementation exception in this app; nothing
    here needs its own try/except."""


class NodryaMemoryBackend:
    """Points the same read/write surface LocalMemoryBackend implements at
    Nodrya (the operator's own real notes app) instead of the local
    `memory` table, for any workspace whose memory_backend setting is
    "nodrya" (2026-09-25).

    Every write goes to Nodrya's write-narrow memory category through its
    MCP surface (mcp_client.py) -- never a direct database connection;
    Nodrya is someone else's live product with other real users. One
    connection value, not two: Nodrya's own connector URL already has its
    auth token baked into the path (POST /api/mcp/<token> -- verified
    against Nodrya's real source, not guessed), so there's no separate
    header-carried credential to manage here, unlike every other provider
    this app talks to. A local
    write-through cache (nodrya_memory, store.py) mirrors every write this
    backend makes, so it's instantly recallable -- independent of Nodrya's
    own async note-embedding job, which runs on its own poll cadence and
    is meant for cross-surface durability, not her turn-by-turn recall. A
    synchronous Nodrya-side embedding call was deliberately rejected: it
    would tie a memory write's success to the embedding provider's live
    uptime and still need the async job as a retry backstop, so it adds
    a failure mode without removing one.

    Scope: this class implements exactly LocalMemoryBackend's interface --
    recall()/context_slice()/etc. cover HER memory (the Nodrya category
    this backend writes to). The original design's "read broadly across
    his OTHER Nodrya notes" is a separate capability (Nodrya's own
    search_by_meaning MCP tool, model-facing) -- not part of this seam,
    and not built here.

    Every write/update/delete either fully succeeds (Nodrya's copy AND the
    local cache both land) or raises NodryaBackendError with the local
    cache untouched -- no method here pretends a Nodrya failure was a
    partial success."""

    def _connection(self, workspace_id: int, *, require_category: bool = True) -> dict | None:
        import crypto  # local: same import-cycle reasoning as mcp_client below
        url_enc = (config.get("workspace", workspace_id, "nodrya_mcp_url") or "").strip()
        category_id = int(config.get("workspace", workspace_id, "nodrya_memory_category_id") or 0)
        if not url_enc or (require_category and category_id <= 0):
            return None
        try:
            url = crypto.decrypt(url_enc)
        except ValueError:
            # Wrong/rotated key file, or a stale plaintext value from before
            # this was encrypted at rest -- either way, not a usable
            # credential, so this is "not configured" rather than a crash.
            return None
        return {"url": url, "category_id": category_id}

    def _require_connection(self, workspace_id: int) -> dict:
        conn = self._connection(workspace_id)
        if conn is None:
            raise NodryaBackendError("Nodrya memory is not configured for this workspace")
        return conn

    def list_categories(self, workspace_id: int) -> list[dict]:
        """Nodrya's own categories, by name -- so the settings page can
        offer a pick-by-name dropdown instead of asking the operator to
        already know a raw Nodrya category id (2026-10-01, operator's own
        ask: "Nodrya doesn't expose category IDs just names"). No category
        needs to be chosen yet to call this -- require_category=False,
        since that's exactly the chicken-and-egg this method exists to
        avoid: the connector URL alone is enough. Verified against
        Nodrya's own source (mcp.php's mcp_tool_list_categories):
        {"count", "categories": [{"id", "name", "color", "parent_id",
        "note_count", "created_at"}]}, and it never calls
        mcp_require_write, so a read-scope connector token works too --
        not that it matters here, since the token isn't a separate
        argument at all, see _connection()'s own docstring."""
        conn = self._connection(workspace_id, require_category=False)
        if conn is None:
            raise NodryaBackendError("Nodrya's connector URL must be saved before browsing categories")
        result = self._call(conn, "list_categories", {})
        return result.get("categories") or []

    def _call(self, conn: dict, name: str, arguments: dict) -> dict:
        import mcp_client  # local: same top-level-cycle reasoning as chat's own local imports below
        try:
            # No Authorization header -- conn["url"] already carries the
            # token in its path (POST /api/mcp/<token>, Nodrya's own
            # auth model, verified against its real source), and Nodrya
            # reads that path token first, a header second. Nothing here
            # has a separate credential left to send.
            result = mcp_client.call_tool(conn["url"], name, arguments)
        except mcp_client.MCPError as exc:
            raise NodryaBackendError(f"Nodrya MCP call to {name} failed: {exc}") from exc
        content = result.get("content") or []
        text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
        if result.get("isError"):
            raise NodryaBackendError(f"Nodrya tool {name} reported an error: {text or '(no detail)'}")
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            raise NodryaBackendError(f"Nodrya tool {name} returned unparseable content: {exc}") from exc

    @staticmethod
    def _title(value: str) -> str:
        # Nodrya notes need a title; the memory category holds one fact per
        # note, so a short prefix of the value is the only sane title --
        # there's no separate "title" concept for a memory otherwise.
        flat = " ".join(value.split())
        return flat[:60] + ("..." if len(flat) > 60 else "")

    @staticmethod
    def _row(r: dict) -> dict:
        d = dict(r)
        try:
            d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
        except (json.JSONDecodeError, TypeError):
            d["tags"] = []
        d["safety_tier"] = bool(d.get("safety_tier"))
        return d

    def write(self, *, user_id: int, workspace_id: int, type_: str, value: str,
             tags: list | None, safety: bool, source: str = "tool") -> int:
        conn = self._require_connection(workspace_id)
        tags_clean = _clean_tags(tags)
        created = self._call(conn, "create_note", {
            "title": self._title(value), "content": value,
            "category_id": conn["category_id"], "tags": tags_clean,
        })
        note_id = (created.get("note") or {}).get("id")
        vector = self._embed(value)
        now = time.time()
        return store.write(lambda c: c.execute(
            "INSERT INTO nodrya_memory(user_id,workspace_id,nodrya_note_id,type,value,tags,source,"
            "safety_tier,created_ts,updated_ts,embedding_json,embedding_model) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, workspace_id, note_id, type_, value, json.dumps(tags_clean), source,
             1 if safety else 0, now, now, json.dumps(vector) if vector else None,
             EMBEDDING_MODEL if vector else None)
        ).lastrowid)

    def update(self, *, user_id: int, memory_id: int, value: str | None,
              tags: list | None, safety: bool | None) -> dict | None:
        row = store.read(lambda c: c.execute(
            "SELECT * FROM nodrya_memory WHERE id=? AND user_id=?", (memory_id, user_id)).fetchone())
        if row is None:
            return None
        conn = self._require_connection(row["workspace_id"])
        new_value = (value or "").strip() or row["value"]
        new_tags = _clean_tags(tags) if tags is not None else json.loads(row["tags"] or "[]")
        new_safety = (1 if safety else 0) if safety is not None else row["safety_tier"]
        if row["nodrya_note_id"] is not None:
            self._call(conn, "update_note", {
                "note_id": row["nodrya_note_id"], "title": self._title(new_value),
                "content": new_value, "tags": new_tags,
            })
        vector = self._embed(new_value)
        now = time.time()
        store.write(lambda c: c.execute(
            "UPDATE nodrya_memory SET value=?,tags=?,updated_ts=?,safety_tier=?,embedding_json=?,embedding_model=? "
            "WHERE id=? AND user_id=?",
            (new_value, json.dumps(new_tags), now, new_safety,
             json.dumps(vector) if vector else None, EMBEDDING_MODEL if vector else None,
             memory_id, user_id)))
        return {"type": row["type"], "value": new_value}

    def set_pinned(self, *, user_id: int, memory_id: int, pinned: bool) -> dict | None:
        row = self.get(user_id=user_id, memory_id=memory_id)
        if row is None:
            return None
        store.write(lambda c: c.execute(
            "UPDATE nodrya_memory SET pinned=? WHERE id=? AND user_id=?",
            (1 if pinned else 0, memory_id, user_id)))
        return {"type": row["type"]}

    def set_safety_tier(self, *, user_id: int, memory_id: int, safety: bool) -> dict | None:
        row = self.get(user_id=user_id, memory_id=memory_id)
        if row is None:
            return None
        store.write(lambda c: c.execute(
            "UPDATE nodrya_memory SET safety_tier=? WHERE id=? AND user_id=?",
            (1 if safety else 0, memory_id, user_id)))
        return {"type": row["type"]}

    def get(self, *, user_id: int, memory_id: int) -> dict | None:
        row = store.read(lambda c: c.execute(
            "SELECT * FROM nodrya_memory WHERE id=? AND user_id=?", (memory_id, user_id)).fetchone())
        return self._row(row) if row is not None else None

    def delete(self, *, user_id: int, memory_id: int) -> dict | None:
        row = store.read(lambda c: c.execute(
            "SELECT * FROM nodrya_memory WHERE id=? AND user_id=?", (memory_id, user_id)).fetchone())
        if row is None:
            return None
        if row["nodrya_note_id"] is not None:
            conn = self._require_connection(row["workspace_id"])
            self._call(conn, "trash_note", {"note_id": row["nodrya_note_id"]})
        store.write(lambda c: c.execute(
            "DELETE FROM nodrya_memory WHERE id=? AND user_id=?", (memory_id, user_id)))
        return {"type": row["type"], "value": row["value"]}

    def recall(self, *, user_id: int, types: list | None, tags: list | None,
              query: str | None, limit: int) -> list[dict]:
        clauses = ["user_id=?"]
        params: list = [user_id]
        if types:
            clauses.append(f"type IN ({','.join('?' * len(types))})")
            params.extend(types)
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("value LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        sql = (f"SELECT * FROM nodrya_memory WHERE {' AND '.join(clauses)} "
              "ORDER BY pinned DESC, updated_ts DESC LIMIT ?")
        params.append(limit)
        rows = store.read(lambda c: c.execute(sql, params).fetchall())

        tag_filter = set(_clean_tags(tags)) if tags else None
        out = []
        for r in rows:
            d = self._row(r)
            if tag_filter and not (tag_filter & set(d["tags"])):
                continue
            out.append(d)
        return out

    def context_slice(self, *, user_id: int, types: tuple) -> list[dict]:
        rows = store.read(lambda c: c.execute(
            f"SELECT * FROM nodrya_memory WHERE user_id=? AND type IN ({','.join('?' * len(types))}) "
            "ORDER BY updated_ts DESC", (user_id, *types)).fetchall())
        return [self._row(r) for r in rows]

    def pinned_rows(self, *, user_id: int) -> list[dict]:
        rows = store.read(lambda c: c.execute(
            "SELECT * FROM nodrya_memory WHERE user_id=? AND pinned=1 ORDER BY updated_ts DESC",
            (user_id,)).fetchall())
        return [self._row(r) for r in rows]

    def all_rows(self, *, user_id: int) -> list[dict]:
        rows = store.read(lambda c: c.execute(
            "SELECT * FROM nodrya_memory WHERE user_id=? ORDER BY type, updated_ts DESC", (user_id,)).fetchall())
        return [self._row(r) for r in rows]

    def safety_rows(self, *, user_id: int) -> list[dict]:
        rows = store.read(lambda c: c.execute(
            "SELECT * FROM nodrya_memory WHERE user_id=? AND safety_tier=1 ORDER BY updated_ts DESC",
            (user_id,)).fetchall())
        return [self._row(r) for r in rows]

    def topic_match(self, *, user_id: int, topics: list, threshold: float, limit: int) -> list[dict]:
        rows = self.all_rows(user_id=user_id)
        return topic_match.rank_keyword(topics, rows, threshold=threshold, limit=limit)

    def _embed(self, text: str) -> list[float] | None:
        """Best-effort local embedding for the write-through cache -- never
        raises. A write/update must not fail just because the embedding
        provider is briefly down; it only means this one entry falls back
        to plain LIKE matching in recall() until the next update recomputes
        it. Same degrade-gracefully contract _embedding_for/semantic_match
        below apply to the safety-tier semantic pass."""
        import chat
        res = chat.openrouter_embed([text], model=EMBEDDING_MODEL)
        if not res.get("ok"):
            print(f"memory.NodryaMemoryBackend._embed: embedding call failed: {res.get('reason')}", flush=True)
            return None
        vectors = res.get("vectors") or []
        return vectors[0] if vectors else None

    def _embedding_for(self, row: dict) -> list[float] | None:
        import chat
        if row.get("embedding_json") and row.get("embedding_model") == EMBEDDING_MODEL:
            try:
                return json.loads(row["embedding_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        res = chat.openrouter_embed([row["value"]], model=EMBEDDING_MODEL)
        if not res.get("ok"):
            print(f"memory.NodryaMemoryBackend._embedding_for: embedding call failed for memory "
                 f"{row['id']}: {res.get('reason')}", flush=True)
            return None
        vec = res["vectors"][0]
        store.write(lambda c: c.execute(
            "UPDATE nodrya_memory SET embedding_json=?, embedding_model=? WHERE id=?",
            (json.dumps(vec), EMBEDDING_MODEL, row["id"])))
        return vec

    def semantic_match(self, *, user_id: int, query_text: str, limit: int) -> tuple[list[dict], bool]:
        import chat
        rows = self.safety_rows(user_id=user_id)
        if not rows:
            return [], False
        q = chat.openrouter_embed([query_text], model=EMBEDDING_MODEL)
        if not q.get("ok"):
            print(f"memory.NodryaMemoryBackend.semantic_match: query embedding failed: "
                 f"{q.get('reason')} -- safety tier check degraded to keyword-only this turn", flush=True)
            return [], True
        qvec = q["vectors"][0]
        scored, any_failed = [], False
        for r in rows:
            vec = self._embedding_for(r)
            if vec is None:
                any_failed = True
                continue
            sim = topic_match.cosine(qvec, vec)
            if sim >= TOPIC_SEMANTIC_THRESHOLD:
                scored.append((sim, r))
        scored.sort(key=lambda t: -t[0])
        matches = [{"id": r["id"], "type": r["type"], "value": r["value"], "safety_tier": True,
                   "score": sim} for sim, r in scored[:limit]]
        return matches, any_failed


_BACKENDS = {"local": LocalMemoryBackend(), "nodrya": NodryaMemoryBackend()}


def _backend_for(workspace_id: int) -> LocalMemoryBackend:
    """Falls back to "local" for anything unrecognized, same posture
    schema_for() already takes for a stale/unknown name -- a typo in the
    setting must never be the reason memory silently stops working."""
    name = config.get("workspace", workspace_id, "memory_backend")
    return _BACKENDS.get(name, _BACKENDS["local"])


def _validate_type(type_: str) -> str | None:
    if type_ not in TYPES:
        return f"unknown type {type_!r} -- must be one of: {', '.join(TYPES)}"
    return None


def _clean_tags(tags) -> list[str]:
    if not tags:
        return []
    if isinstance(tags, str):
        tags = [tags]
    out = []
    for t in tags[:MAX_TAGS]:
        t = str(t).strip().lower()[:MAX_TAG_LEN]
        if t:
            out.append(t)
    return out


def _row_out(r: dict) -> dict:
    d = dict(r)
    try:
        d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
    except (json.JSONDecodeError, TypeError):
        d["tags"] = []
    d["safety_tier"] = bool(d.get("safety_tier"))
    return d


def _log(memory_id: int, user_id: int, action: str, *, actor: str = "nori", type_: str | None = None,
         value: str | None = None, note: str | None = None) -> None:
    """actor defaults to "nori" -- every EXISTING call site is her own
    deliberate tool call, unchanged. reflect() passes actor="reflection";
    the settings-page review action (resolve_removal_flag) passes
    actor="user" -- the same three-way provenance a sibling application's memory_events
    already distinguished, extended here rather than reinvented. "peer"
    (2026-09-13, operator's own ask) is the fourth: a fact recorded during
    a peer-triggered turn (see _actor_for) isn't really "her own" tool
    call the way one made in a live conversation with him is -- it needs
    to be tellable apart later, the same reason the other three already are."""
    store.write(lambda c: c.execute(
        "INSERT INTO memory_events(ts, memory_id, user_id, action, actor, type, value, note) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (time.time(), memory_id, user_id, action, actor, type_, value, note)))


def _actor_for(session: dict) -> str:
    """'peer' when this tool call is happening inside a peer-triggered
    turn (peers.py's _run_prompted_turn sets session["_peer_context"] to
    the peer's name for exactly this), else the ordinary 'nori' default
    -- a fact learned because a peer said something isn't provenance-
    identical to one that came up in a live conversation with him."""
    return "peer" if session.get("_peer_context") else "nori"


# ── tool implementations (registered with tools.py below) ───────────────
def _remember(session: dict, type: str, value: str, tags: list | None = None,
             safety: bool = False) -> dict:
    err = _validate_type(type)
    if err:
        return {"error": err}
    value = (value or "").strip()
    if not value:
        return {"error": "value can't be empty"}
    backend = _backend_for(session["workspace_id"])
    mid = backend.write(user_id=session["user_id"], workspace_id=session["workspace_id"],
                        type_=type, value=value, tags=tags, safety=safety)
    _log(mid, session["user_id"], "add", actor=_actor_for(session), type_=type, value=value)
    return {"ok": True, "memory_id": mid}


def _update_memory(session: dict, memory_id: int, value: str | None = None,
                   tags: list | None = None, safety: bool | None = None) -> dict:
    backend = _backend_for(session["workspace_id"])
    updated = backend.update(user_id=session["user_id"], memory_id=memory_id,
                             value=value, tags=tags, safety=safety)
    if updated is None:
        return {"error": "no such memory"}
    _log(memory_id, session["user_id"], "update", actor=_actor_for(session),
        type_=updated["type"], value=updated["value"])
    return {"ok": True, "memory_id": memory_id}


def _forget(session: dict, memory_id: int) -> dict:
    """Peer-triggered forgets (2026-09-15, operator's own explicit ask)
    never delete outright -- they flag_removal instead, the same
    non-destructive path reflection's own removal candidates already use
    (see removal_candidates()/resolve_removal_flag() below), reviewed on
    the memory settings tab exactly like a reflection flag is. Reasoning
    is recoverability, not permission: a peer asking through the trust
    system to forget something is already authorized to ask -- the point
    isn't to gate the request, it's that the choice she makes on a peer's
    behalf should stay observable rather than erasing the evidence of
    itself. Her own direct forget() call, in a live conversation with
    him, is unchanged -- that's still a real, immediate delete, same as
    it's always been."""
    backend = _backend_for(session["workspace_id"])
    row = backend.get(user_id=session["user_id"], memory_id=memory_id)
    if row is None:
        return {"error": "no such memory"}
    actor = _actor_for(session)
    if actor == "peer":
        _log(memory_id, session["user_id"], "flag_removal", actor="peer", type_=row["type"],
            value=row["value"], note="a connected peer asked to forget this")
        return {"ok": True, "flagged": True,
                "note": "flagged for review on the memory settings tab rather than deleted "
                        "outright -- a peer-requested forget doesn't delete immediately"}
    deleted = backend.delete(user_id=session["user_id"], memory_id=memory_id)
    _log(memory_id, session["user_id"], "remove", actor=actor, type_=deleted["type"], value=deleted["value"])
    return {"ok": True}


def set_pinned(user_id: int, memory_id: int, pinned: bool = True) -> dict:
    """Real pin/unpin, shared by the model's own tool call and the settings
    UI (2026-09-12) -- one path, so a pin made either way is identical and
    identically logged. Previously only reachable via the model deciding
    to call pin_memory, which -- same as set_emotion before today's fix --
    never actually happened in real use; the UI action below doesn't
    replace the tool, it gives the operator a way to do it directly
    instead of waiting on the model to think of it."""
    updated = _backend_for_user(user_id).set_pinned(user_id=user_id, memory_id=memory_id, pinned=pinned)
    if updated is None:
        return {"error": "no such memory"}
    _log(memory_id, user_id, "pin" if pinned else "unpin", type_=updated["type"])
    return {"ok": True}


def _pin_memory(session: dict, memory_id: int, pinned: bool = True) -> dict:
    return set_pinned(session["user_id"], memory_id, pinned)


def set_safety_tier(user_id: int, memory_id: int, safety: bool) -> dict:
    """The operator's half of marking a memory safety/constraint-tier --
    same shared shape as set_pinned above (settings-page action AND the
    model's own remember/update_memory 'safety' argument end up here...
    actually the model's path writes the column directly in _remember/
    _update_memory, same reasoning those already had for tags/value: one
    write, not a write-then-a-second-call). This is the settings-tab path
    specifically, for retroactively marking an EXISTING memory the model
    saved before this feature existed, or correcting one it got wrong --
    a human's judgment call about what's really a boundary, not an
    algorithm's guess (see topic_match.py's own docstring on why this
    is never inferred from content)."""
    updated = _backend_for_user(user_id).set_safety_tier(user_id=user_id, memory_id=memory_id, safety=safety)
    if updated is None:
        return {"error": "no such memory"}
    _log(memory_id, user_id, "safety_tier_on" if safety else "safety_tier_off", type_=updated["type"])
    return {"ok": True}


def delete_memory(user_id: int, memory_id: int) -> dict:
    """Real, immediate delete -- the settings page's own direct action
    (2026-09-30, operator's own ask: "I want to be able to delete/edit
    Nori's memories"). Same non-peer-gated path _forget()'s own docstring
    already calls out as the normal case: only a PEER-triggered forget
    flags for review instead of deleting outright; this is the operator
    acting directly on their own settings page, exactly the "her own
    direct forget() call... is unchanged -- still a real, immediate
    delete" case that docstring describes, just reached from a button
    instead of a live conversation."""
    backend = _backend_for_user(user_id)
    row = backend.get(user_id=user_id, memory_id=memory_id)
    if row is None:
        return {"error": "no such memory"}
    deleted = backend.delete(user_id=user_id, memory_id=memory_id)
    _log(memory_id, user_id, "remove", actor="user", type_=deleted["type"], value=deleted["value"])
    return {"ok": True}


def edit_memory(user_id: int, memory_id: int, value: str) -> dict:
    """Real, immediate edit of a memory's own text -- settings-page
    action (2026-09-30), same backend.update() the model's own
    update_memory tool call already uses; tags/safety_tier untouched
    (backend.update()'s own None-means-unchanged contract)."""
    value = (value or "").strip()
    if not value:
        return {"error": "value can't be empty"}
    updated = _backend_for_user(user_id).update(user_id=user_id, memory_id=memory_id,
                                                value=value, tags=None, safety=None)
    if updated is None:
        return {"error": "no such memory"}
    _log(memory_id, user_id, "update", actor="user", type_=updated["type"], value=updated["value"])
    return {"ok": True}


def migrate_local_to_nodrya(workspace_id: int) -> dict:
    """One-time (repeatable, idempotent) bulk copy of every LOCAL memory
    row into Nodrya -- for a household switching memory_backend over to
    "nodrya" after already accumulating real history under "local"
    (2026-10-01, operator's own ask). Independent of the active
    memory_backend setting: this always writes through
    NodryaMemoryBackend directly, regardless of which backend is
    currently selected, so it can run BEFORE flipping the switch to
    confirm the data actually lands before local reads stop being what
    the rest of the app sees.

    Idempotent by construction, not a special migrated-flag column: each
    row this successfully copies gets a memory_events action="migrate"
    entry against its LOCAL id (the same audit table every other
    settings-page action already writes to), and a second run skips
    anything already carrying one -- safe to click twice, or to retry
    after a partial failure, without creating duplicate Nodrya notes.

    One real Nodrya API call per row (create_note) -- a failure on one
    row is recorded and the rest continue; this is never all-or-nothing,
    since each row that succeeds is already its own independent write by
    the time the next one starts."""
    nodrya = NodryaMemoryBackend()
    if nodrya._connection(workspace_id) is None:
        return {"ok": False, "error": "Nodrya isn't configured for this workspace yet -- "
                "fill in the connection above first"}
    local = LocalMemoryBackend()
    migrated, skipped, failed = 0, 0, []
    for user in accounts.list_users(workspace_id):
        uid = user["id"]
        already = {r["memory_id"] for r in store.read(lambda c: c.execute(
            "SELECT DISTINCT memory_id FROM memory_events WHERE user_id=? AND action='migrate'",
            (uid,)).fetchall())}
        for row in local.all_rows(user_id=uid):
            if row["id"] in already:
                skipped += 1
                continue
            try:
                new_id = nodrya.write(user_id=uid, workspace_id=workspace_id, type_=row["type"],
                                      value=row["value"], tags=row["tags"],
                                      safety=row["safety_tier"], source="migration")
                if row.get("pinned"):
                    nodrya.set_pinned(user_id=uid, memory_id=new_id, pinned=True)
            except NodryaBackendError as exc:
                failed.append({"user_id": uid, "memory_id": row["id"], "error": str(exc)})
                continue
            _log(row["id"], uid, "migrate", actor="user", type_=row["type"],
                note=f"copied to nodrya as memory_id {new_id}")
            migrated += 1
    return {"ok": True, "migrated": migrated, "skipped": skipped, "failed": failed}


def _recall(session: dict, types: list | None = None, tags: list | None = None,
           query: str | None = None, limit: int = 20) -> dict:
    limit = max(1, min(int(limit or 20), 100))
    bad = [t for t in (types or []) if t not in TYPES]
    if bad:
        return {"error": f"unknown type(s) {bad} -- must be one of: {', '.join(TYPES)}"}

    backend = _backend_for(session["workspace_id"])
    out = backend.recall(user_id=session["user_id"], types=types, tags=tags, query=query, limit=limit)

    result = {"memories": [{"id": d["id"], "type": d["type"], "value": d["value"],
                            "tags": d["tags"], "pinned": bool(d["pinned"]),
                            "safety": d["safety_tier"]} for d in out]}

    # Broad Nodrya retrieval (opt-in, see nodrya_broad_search's own
    # docstring) -- a second, separate list, deliberately never merged
    # into "memories" above: these are raw notes from the operator's
    # whole Nodrya account, not curated facts she wrote herself. No-op
    # ([], False) when the setting is off or query is empty, so this is
    # safe to leave in the call unconditionally.
    notes, degraded = nodrya_broad_search(session["workspace_id"], query or "", limit=min(limit, 10))
    if notes:
        result["notes_from_nodrya"] = [
            {"title": n.get("title") or "Untitled note",
             "category": (n.get("category") or {}).get("name"),
             "excerpt": ("(content is end-to-end encrypted -- not readable here)" if n.get("is_encrypted")
                        else " ".join((n.get("content") or "").split())[:400]),
             "similarity": n.get("similarity"), "url": n.get("url")} for n in notes]
    elif degraded:
        result["notes_from_nodrya_degraded"] = "couldn't reach Nodrya to search broadly this time"
    return result


# ── always-on context slice ──────────────────────────────────────────────
def context_block(user_id: int) -> str:
    """A small, hard-capped slice injected into every system prompt -- NOT
    the whole store. The always-load types (identity/preference) by
    recency, stopping once the char budget is spent. Pinned rows are
    handled separately now (see pins_precheck_line() / precheck.py) --
    everything else only ever reaches the model via recall()."""
    user = accounts.get_user(user_id)
    budget = (config.get("workspace", user["workspace_id"], "memory_max_tokens") if user else 375) * 4
    # No user row (an edge case the original raw-SQL version never gated
    # on either) still reads from local -- there's no workspace_id to
    # resolve a backend selection with, and the original behavior was to
    # query anyway, using whatever user_id it was given.
    backend = _backend_for(user["workspace_id"]) if user else _BACKENDS["local"]
    rows = backend.context_slice(user_id=user_id, types=ALWAYS_LOAD_TYPES)
    lines, used = [], 0
    for d in rows:
        line = f"- [{d['type']}] {d['value']}"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    return "WHAT YOU KNOW ABOUT THEM (facts, not lines to recite):\n" + "\n".join(lines)


def _backend_for_user(user_id: int) -> LocalMemoryBackend:
    """Same reasoning as emotion.get_state()'s own internal lookup: the
    public signature here is (and stays) user_id-only, so this is the one
    place that pays the accounts lookup rather than widening every one of
    these callers to also thread workspace_id through."""
    user = accounts.get_user(user_id)
    return _backend_for(user["workspace_id"]) if user else _BACKENDS["local"]


def all_rows(user_id: int) -> list[dict]:
    """Every memory row for this user, any type -- reflection's own view of
    "what's already stored" (fed back to the model so it can update/skip
    instead of duplicating), and the settings-page memory tab's listing.
    Never used for the per-turn context slice -- that's context_block()'s
    job, capped and type-filtered; this is deliberately unbounded."""
    return _backend_for_user(user_id).all_rows(user_id=user_id)


def safety_rows(user_id: int) -> list[dict]:
    """Just the safety/constraint tier -- what the semantic pass runs
    against (both the ordinary per-turn low-confidence fallback and
    preaction_check's own unconditional check). Small by construction:
    this is meant to be a few deliberately-flagged boundaries, not most
    of the store, so scoring/embedding all of them every time is cheap."""
    return _backend_for_user(user_id).safety_rows(user_id=user_id)


# ── reflection -- infers memory from conversation instead of waiting for
# her to call remember() herself. Ported from a sibling application's memory.reflect()
# (same day, same problem: give her a memory that fills itself in), with
# the differences that actually matter for a TYPED store:
#
#   1. A sibling application dumps its whole memory into every context; a mistyped
#      entry there is untidy. Nori's memory is retrieved BY TYPE on
#      demand (context_block/recall) -- a mistyped entry here doesn't
#      surface at all, ever, until someone happens to recall() the wrong
#      type and notices. That's why _REFLECT_SYS is written to bias hard
#      toward "skip it" over "guess a type", where a sibling application's prompt
#      only had to bias toward "skip it" over "record trivia."
#   2. Per-user, not singleton -- every call here takes a user_id, reads
#      only that user's conversation, writes only that user's memory.
#   3. Model-driven dedup, same mechanism as a sibling application: the current
#      memory (with each row's own `source`) rides along in the prompt,
#      and the instructions say never to duplicate what's already there,
#      update it in place instead.
#   4. A sibling application's reflection can delete memory outright (a bare id list,
#      applied with no further gate beyond a valid id). Deliberately NOT
#      ported: this pass only ever adds or updates. A candidate for
#      removal is logged (memory_events, action="flag_removal") for a
#      human to review on the settings page, never acted on here: an
#      incorrect auto-delete is silent and hard to notice, so removal
#      stays a human call.
_REFLECT_SYS = ("""You maintain a TYPED long-term memory store about one person, for their assistant.
You are given their CURRENT MEMORY (a JSON list -- id/type/value/tags/pinned/source for each row)
and the RECENT CONVERSATION. Return ONLY a JSON object with keys: add, update, flag_removal.

This memory is retrieved BY TYPE, on demand -- there is no "browse everything" view feeding every
reply. A fact filed under the wrong type doesn't just look untidy: it effectively never surfaces
again. Getting the type right matters more than catching more facts. If you are not confident
which type below a fact belongs to, DO NOT add it -- skip it rather than guess.

Valid types, exactly these, nothing else:
""" + "\n".join(f"- {t}: {TYPE_HELP[t]}" for t in TYPES) + """

- add: list of {type, value, tags}. type MUST be one of the types above and you must be genuinely
  confident of it -- if unsure, leave the fact out entirely. Only add durable facts worth
  remembering weeks from now, not small talk, and not anything already covered by an existing
  memory below. value: the fact, plainly stated, under 30 words. tags: a few short lowercase
  words, optional.
- update: list of {id, value} -- an EXISTING memory (by id, from CURRENT MEMORY) whose content
  changed or should be refined. Never used to fix a wrong type: if an existing row's TYPE is
  wrong, leave it alone and use flag_removal on it instead (a differently-typed add can follow,
  once you're confident).
- flag_removal: list of {id, reason} -- this pass never deletes anything itself. If an existing
  memory looks wrong, superseded, mis-typed, or no longer true, name it here with a short reason
  for a human to review. Do not act on it yourself, and do not also add a duplicate replacement
  for something you've flagged unless you're confident of the correct type.

Each existing memory carries a "source": "tool" means she (or a past reflection pass) recorded or
updated it deliberately. Never propose add for something already covered by an existing memory
regardless of source -- check the current list first. If the conversation only confirms or
slightly refines an existing fact, use update on that same id instead of adding a new one.

Be conservative. Prefer few, correctly-typed, high-value memories over broad coverage. Return
{"add":[],"update":[],"flag_removal":[]} if nothing warrants a change.""")


def due_for_reflection(user_id: int) -> bool:
    import conversation  # local: same reasoning as every other cross-module import here
    last_refl = _last_reflection_ts(user_id)
    since_refl = conversation.count_since_ts(user_id, last_refl)
    if since_refl >= REFLECTION_EVERY_MSGS:
        return True
    last_user_ts = conversation.last_user_message_ts(user_id)
    if last_user_ts is None:
        return False
    gap_h = (time.time() - last_user_ts) / 3600
    return gap_h >= REFLECTION_GAP_HOURS and since_refl > 0


def _last_reflection_ts(user_id: int) -> float:
    r = store.read(lambda c: c.execute(
        "SELECT last_reflection_ts FROM memory_reflection_state WHERE user_id=?", (user_id,)).fetchone())
    return r["last_reflection_ts"] if r else 0.0


def _set_last_reflection_ts(user_id: int, ts: float) -> None:
    def _w(c):
        row = c.execute(
            "SELECT 1 FROM memory_reflection_state WHERE user_id=?", (user_id,)).fetchone()
        if row:
            c.execute("UPDATE memory_reflection_state SET last_reflection_ts=? WHERE user_id=?", (ts, user_id))
        else:
            c.execute("INSERT INTO memory_reflection_state(user_id, last_reflection_ts) VALUES (?,?)",
                     (user_id, ts))
    store.write(_w)


def reflect(user_id: int) -> dict:
    """The reflection pass itself. Never raises -- a model/JSON failure
    comes back as {"error": ...} and last_reflection_ts is deliberately
    NOT advanced on failure, so the same backlog is retried next cycle
    rather than silently dropped (same reasoning as a sibling application's)."""
    import accounts  # local: avoids a needless top-level dependency direction
    import chat
    import conversation

    window = max(30, REFLECTION_EVERY_MSGS * 3)
    msgs = conversation.recent(user_id, limit=window)
    if not msgs:
        return {"added": 0, "updated": 0, "flagged": 0, "skipped": "no messages"}

    import own_output   # her own past words are scrubbed before they are shown back to a model (see own_output.py)
    convo = "\n".join(f'{"them" if m["role"] == "user" else "her"}: {t}' for m in msgs for t in [own_output.scrub(m["content"], m["role"])] if t or m["role"] == "user")
    backend = _backend_for_user(user_id)
    mem = [{"id": d["id"], "type": d["type"], "value": d["value"], "tags": d["tags"],
            "pinned": d["pinned"], "source": d["source"]} for d in backend.all_rows(user_id=user_id)]
    user_msg = f"CURRENT MEMORY:\n{json.dumps(mem, ensure_ascii=False)}\n\nRECENT CONVERSATION:\n{convo}"

    try:
        out = chat.call([{"role": "system", "content": _REFLECT_SYS},
                         {"role": "user", "content": user_msg}],
                        want_json=True, max_tokens=1200, temperature=0.2)
        delta = json.loads(out["content"] or "{}")
    except (chat.ModelError, json.JSONDecodeError) as exc:
        return {"error": str(exc)}

    user = accounts.get_user(user_id)
    workspace_id = user["workspace_id"] if user else None
    now = time.time()
    added = updated = flagged = 0

    for a in delta.get("add", []) or []:
        try:
            type_ = a["type"]
            if type_ not in TYPES:
                continue  # never guess -- an invalid/uncertain type is dropped, not coerced
            value = str(a["value"]).strip()[:400]
            if not value:
                continue
            mid = backend.write(user_id=user_id, workspace_id=workspace_id, type_=type_,
                               value=value, tags=a.get("tags"), safety=False, source="reflection")
            _log(mid, user_id, "add", actor="reflection", type_=type_, value=value)
            added += 1
        except (KeyError, TypeError, ValueError):
            pass

    for u in delta.get("update", []) or []:
        try:
            mid = int(u["id"])
            value = str(u["value"]).strip()[:400]
            if not value:
                continue
            updated_row = backend.update(user_id=user_id, memory_id=mid, value=value,
                                         tags=None, safety=None)
            if updated_row is None:
                continue
            _log(mid, user_id, "update", actor="reflection", type_=updated_row["type"], value=value)
            updated += 1
        except (KeyError, TypeError, ValueError):
            pass

    for f in delta.get("flag_removal", []) or []:
        try:
            mid = int(f["id"])
            row = backend.get(user_id=user_id, memory_id=mid)
            if row is None:
                continue
            reason = str(f.get("reason", ""))[:200]
            _log(mid, user_id, "flag_removal", actor="reflection", type_=row["type"],
                value=row["value"], note=reason)
            flagged += 1
        except (KeyError, TypeError, ValueError):
            pass

    _set_last_reflection_ts(user_id, now)
    return {"added": added, "updated": updated, "flagged": flagged}


def removal_candidates(user_id: int, limit: int = 50) -> list[dict]:
    """Flags still awaiting human review -- a flag only shows up here if
    it's still the MOST RECENT event for that memory row. Any later touch
    (an update, a pin, a dismiss, the row being forgotten outright) is
    treated as the flag having been handled, without needing a dedicated
    "resolved" column -- the audit trail already carries that information
    once you ask "what's the last thing that happened to this row.\""""
    rows = store.read(lambda c: c.execute(
        "SELECT e.* FROM memory_events e JOIN memory m ON m.id = e.memory_id AND m.user_id = e.user_id "
        "WHERE e.user_id=? AND e.action='flag_removal' "
        "AND e.ts = (SELECT MAX(ts) FROM memory_events e2 "
        "            WHERE e2.memory_id = e.memory_id AND e2.user_id = e.user_id) "
        "ORDER BY e.ts DESC LIMIT ?", (user_id, limit)).fetchall())
    return [dict(r) for r in rows]


def resolve_removal_flag(user_id: int, memory_id: int, action: str) -> dict:
    """The human half of "surface removal candidates, don't act on them" --
    called only from the settings-page review action, never by reflection
    itself. action='remove' deletes the row for real (same effect as her
    own forget() tool, just attributed to the human); action='dismiss'
    logs a no-op event so this flag stops reappearing in the review list
    without touching the memory itself (see removal_candidates' "most
    recent event wins" logic)."""
    row = store.read(lambda c: c.execute(
        "SELECT * FROM memory WHERE id=? AND user_id=?", (memory_id, user_id)).fetchone())
    if row is None:
        return {"error": "no such memory"}
    if action == "remove":
        store.write(lambda c: c.execute(
            "DELETE FROM memory WHERE id=? AND user_id=?", (memory_id, user_id)))
        _log(memory_id, user_id, "remove", actor="user", type_=row["type"], value=row["value"])
    elif action == "dismiss":
        _log(memory_id, user_id, "flag_dismissed", actor="user", type_=row["type"], value=row["value"])
    else:
        return {"error": "action must be 'remove' or 'dismiss'"}
    return {"ok": True}


# ── tool registration ────────────────────────────────────────────────────
def _register_tools() -> None:
    import tools  # local: avoids a needless top-level dependency direction; no cycle either way

    tools.register(tools.Tool(
        "remember",
        {"type": "function", "function": {
            "name": "remember",
            "description": ("Save a new fact worth keeping, the moment it actually matters -- "
                            "don't wait for a reflection pass that doesn't exist yet. Pick the "
                            "type closest to what the fact is actually about."),
            "parameters": {"type": "object", "properties": {
                "type": {"type": "string", "enum": list(TYPES)},
                "value": {"type": "string", "description": "The fact itself, plainly stated."},
                "tags": {"type": "array", "items": {"type": "string"},
                        "description": "Optional, a few short lowercase tags for finer filtering later."},
                "safety": {"type": "boolean", "default": False,
                          "description": ("Set true ONLY for a real boundary or constraint -- a "
                                          "must/must-not, a permission or access limit, something "
                                          "that should override an ordinary preference if they ever "
                                          "conflict. This is a deliberate call you make, not a guess "
                                          "-- it gets checked automatically, and unconditionally, "
                                          "right before a consequential action (changing an "
                                          "integration or permission, smart-home control, sending a "
                                          "message, changing a setting), so leave it false for "
                                          "anything that's just a preference.")}},
                "required": ["type", "value"]}}},
        _remember, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "update_memory",
        {"type": "function", "function": {
            "name": "update_memory",
            "description": "Correct or refine a fact you already recorded, by its memory_id.",
            "parameters": {"type": "object", "properties": {
                "memory_id": {"type": "integer"},
                "value": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "safety": {"type": "boolean",
                          "description": "Reclassify as a safety/constraint memory (or back out of "
                                        "it) -- see remember's own safety argument. Omit to leave "
                                        "it as it already is."}},
                "required": ["memory_id"]}}},
        _update_memory, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "forget",
        {"type": "function", "function": {
            "name": "forget",
            "description": "Permanently remove a fact by its memory_id -- it no longer matters or was "
                           "wrong. If this is happening because a connected peer asked you to, through "
                           "peer{id}_act, it won't actually delete -- it flags the fact for the operator "
                           "to review instead, and tells you so in the result.",
            "parameters": {"type": "object", "properties": {
                "memory_id": {"type": "integer"}}, "required": ["memory_id"]}}},
        _forget, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "pin_memory",
        {"type": "function", "function": {
            "name": "pin_memory",
            "description": "Mark a fact as always worth keeping in context, or unmark it.",
            "parameters": {"type": "object", "properties": {
                "memory_id": {"type": "integer"},
                "pinned": {"type": "boolean", "default": True}}, "required": ["memory_id"]}}},
        _pin_memory, min_role="member", data_scope="self", risk_tier="B"))

    tools.register(tools.Tool(
        "recall",
        {"type": "function", "function": {
            "name": "recall",
            "description": ("Look up facts relevant to what you're doing right now, instead of "
                            "relying only on the small always-loaded slice. Filter by type "
                            "and/or a free-text search. If broad Nodrya retrieval is enabled and "
                            "`query` is set, the result may also include a notes_from_nodrya list "
                            "-- raw notes from across the operator's whole Nodrya account, not "
                            "curated memory, surfaced because they matched the query."),
            "parameters": {"type": "object", "properties": {
                "types": {"type": "array", "items": {"type": "string", "enum": list(TYPES)}},
                "tags": {"type": "array", "items": {"type": "string"}},
                "query": {"type": "string", "description": "Free-text substring search over the fact itself."},
                "limit": {"type": "integer", "default": 20}}}}},
        _recall, min_role="member", data_scope="self", risk_tier="A"))


def register_peer_actions() -> None:
    """Peer-requestable registration: recall/forget/pin_memory, NOT
    remember/update_memory -- those are her own observations to record,
    not something a peer asking through her makes sense for.

    Deliberately NOT called from inside _register_tools() above, at this
    module's own import time (2026-09-15, found the hard way, with a
    real test, not assumed): memory.py is a transitive dependency of
    peers.py itself (peers.py's own top-level `import chat` pulls in
    chat -> context -> memory, before peers.py has defined
    register_peer_requestable at all) -- so `import peers` from inside
    _register_tools() only succeeds by accident of which module happens
    to trigger that chain first. server.py's own import order (chat
    before peers) happened to make this work, but a direct `import
    peers` anywhere else -- confirmed with exactly that -- hits
    "partially initialized module 'peers' has no attribute
    'register_peer_requestable'". Called once from server.py's main(),
    after every top-level import has already completed -- the same safe
    point mcp_servers.register_all()/peers.register_all() already use
    for their own post-import wiring, not a new pattern.

    Gated on the STANDARD trust ladder (§11.1), the same one
    mcp_servers.py's connection toggles use -- deliberately NOT
    full_trust_only the way get_settings/update_settings are. Reasoning:
    full_trust_only exists for actions judged to need MORE than ordinary
    standing trust (settings_tool.py's own docstring: "there is no queue
    to build" for something full-trust-only by design). forget and
    pin_memory are real, permanent-context-shaping actions, but not a
    different CLASS of action from disabling an MCP connection -- both
    are "a full-trust peer can materially change this app's state,"
    which is exactly what the standard ladder already exists to gate:
    'prompt' holds it for the operator's own approval before anything
    happens, 'none' refuses outright, and only 'full' -- an explicit,
    deliberate grant -- skips that gate. The connected peer is at full
    trust right now, so all three are live for it immediately on deploy,
    same as the MCP toggles already are.

    forget specifically is NOT actually permanent through this path any
    more (see _forget's own docstring above) -- a peer-triggered forget
    flag_removals instead of deleting, so the authority question above
    (should a full-trust peer be able to ask for this at all) stays
    separate from the recoverability one (can it be undone after the
    fact): trust gates whether the request is honored at all, flag_removal
    is what "honored" actually does once it's inside her own memory."""
    import peers
    peers.register_peer_requestable("recall")
    peers.register_peer_requestable("forget")
    peers.register_peer_requestable("pin_memory")


_register_tools()


# ── pre-reply checklist entry (see precheck.py) ─────────────────────────
def pins_precheck_line(session: dict, user_id: int) -> str | None:
    """Pinned facts, proximate -- the explicit "this always matters" signal
    a pin represents deserves the same end-of-context treatment the
    emotion check gets, not a spot buried mid-way through a long standing
    system prompt. Most-recently-pinned first, hard-capped by
    PINNED_CONTEXT_BUDGET_CHARS same as context_block()'s own always-load
    slice -- pins are user-added and otherwise unbounded, and this rides
    on every single real turn, so an unlimited budget here would be a real,
    recurring cost, not a one-time one. Returns None (adds nothing) when
    there are no pins -- an empty checklist entry would itself be the kind
    of standing noise this mechanism exists to avoid."""
    budget = config.get("workspace", session["workspace_id"], "memory_pinned_max_tokens") * 4
    rows = _backend_for(session["workspace_id"]).pinned_rows(user_id=user_id)
    if not rows:
        return None
    lines, used = [], 0
    for d in rows:
        line = f"[{d['type']}] {d['value']}"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return None
    return "Pinned facts (always relevant, kept in mind regardless of topic): " + "; ".join(lines)


# ── topic-triggered activation (2026-09-17) ──────────────────────────────
def semantic_safety_matches(user_id: int, text: str, *, limit: int = 3) -> tuple[list[dict], bool]:
    """Unconditional semantic pass over this user's safety/constraint tier
    -- never gated on keyword confidence (see topic_activation_line and
    preaction_check below for the two different confidence postures they
    each take with this same function). Returns (matches, degraded);
    degraded=True means the embedding provider failed and matches may be
    incomplete or empty for a reason OTHER than "genuinely nothing
    relevant" -- callers must surface that distinction, never let it look
    like a clean all-clear (see chat.openrouter_embed's own docstring).

    Thin delegation to the backend (2026-09-25) -- the actual embed/cache/
    compare logic is LocalMemoryBackend.semantic_match(), moved verbatim."""
    return _backend_for_user(user_id).semantic_match(user_id=user_id, query_text=text, limit=limit)


def _format_matches(hits: list[dict]) -> list[str]:
    lines, used = [], 0
    for h in hits:
        tag = "SAFETY/CONSTRAINT: " if h.get("safety_tier") else ""
        line = f"{tag}[{h['type']}] {h['value']}"
        if used + len(line) > TOPIC_INJECT_CHAR_BUDGET:
            break
        lines.append(line)
        used += len(line)
    return lines


# ── broad Nodrya retrieval (2026-10-02) ──────────────────────────────────
# Deliberately a separate axis from memory_backend/NodryaMemoryBackend
# above: those govern where HER OWN memory writes/reads land (one
# write-narrow category, local or Nodrya). This is "let her read broadly
# across every OTHER note in the operator's Nodrya account too" -- exactly
# the capability NodryaMemoryBackend's own class docstring originally
# scoped OUT ("a separate capability... not part of this seam, and not
# built here") until the operator asked for it directly. Works
# independently of which memory_backend is active, and needs only the
# connector URL, never a category -- "all categories" is the whole point.
def nodrya_broad_search(workspace_id: int, query: str, *, limit: int = 5) -> tuple[list[dict], bool]:
    """Nodrya's own search_by_meaning tool, no category filter -- every
    note across the operator's whole account, ranked by semantic
    similarity to `query`. Gated on nodrya_broad_retrieval (off by
    default: a live per-call network hit to a third party, opt-in, not
    implied by having a connector saved). Returns ([], False) when
    disabled, not configured, or given an empty query -- "nothing to
    show" is the common case, not an error. Returns (matches, True) on
    a real Nodrya-side failure -- best-effort, same (matches, degraded)
    contract as semantic_safety_matches: never raises, never blocks a
    turn on a Nodrya outage."""
    if not config.get("workspace", workspace_id, "nodrya_broad_retrieval"):
        return [], False
    query = (query or "").strip()
    if not query:
        return [], False
    nodrya = NodryaMemoryBackend()
    conn = nodrya._connection(workspace_id, require_category=False)
    if conn is None:
        return [], False
    try:
        result = nodrya._call(conn, "search_by_meaning", {"query": query, "limit": limit})
    except NodryaBackendError as exc:
        print(f"memory.nodrya_broad_search: Nodrya search failed: {exc}", flush=True)
        return [], True
    return result.get("notes") or [], False


def _format_note_matches(notes: list[dict]) -> list[str]:
    """Same char-budget-truncated line-list shape as _format_matches, for
    nodrya_broad_search results -- kept as a SEPARATE formatter rather
    than folded into _format_matches: a raw Nodrya note was never
    curated into her memory taxonomy (no `type` from TYPES, no
    safety_tier), so it needs its own, clearly-labeled presentation, not
    passed off as one of her own memory facts."""
    lines, used = [], 0
    for n in notes:
        title = (n.get("title") or "").strip() or "Untitled note"
        category = ((n.get("category") or {}).get("name") or "").strip()
        where = f" ({category})" if category else ""
        if n.get("is_encrypted"):
            excerpt = "(content is end-to-end encrypted -- not readable here)"
        else:
            excerpt = " ".join((n.get("content") or "").split())[:200]
        line = f'"{title}"{where}: {excerpt}' if excerpt else f'"{title}"{where}'
        if used + len(line) > TOPIC_INJECT_CHAR_BUDGET:
            break
        lines.append(line)
        used += len(line)
    return lines


# One entry per user: the id of the latest user message topic-activation
# has already run for -- keeps a scheduler tick, forced check-in, or peer
# turn from re-running (and re-injecting, re-billing an embedding call)
# every time a turn happens to fire with nothing new actually said since.
# In-memory, not persisted: a restart just means the next turn re-activates
# once more, which is harmless (same content, same decision), unlike
# losing it forever would be.
_activated_mark: dict[int, int] = {}


def topic_activation_line(session: dict, user_id: int) -> str | None:
    """precheck.py-registered (see _register_precheck) -- fires once per
    NEW real inbound message (conversation.latest_user_message_after),
    not once per turn, so a scheduler/peer-motivated turn that runs with
    no fresh message since the last activation adds nothing. Point 1-5 of
    the operator's own spec: extract topics from the message, score
    against tags+content (topic_match.rank_keyword), and for anything
    that ISN'T already a confident keyword hit, layer in the unconditional-
    for-the-safety-tier semantic pass -- measured recall numbers showed
    running keyword-first, semantic-second beats semantic-always (cost)
    or keyword-only (the real miss that measurement found).

    A second, independent block (2026-10-02, operator's own ask) layers
    in nodrya_broad_search -- her own memory above is unaffected either
    way, this never touches the `if not topics` early-exit's existing
    behavior, it just isn't gated behind it, since a broad note search
    runs off the raw message text, not the extracted topic list."""
    import conversation  # local: same reasoning as due_for_reflection's own import
    msg = conversation.latest_user_message_after(user_id, 0)
    if msg is None or _activated_mark.get(user_id) == msg["id"]:
        return None
    _activated_mark[user_id] = msg["id"]
    text = (msg.get("content") or "").strip()
    if not text:
        return None

    mem_block = None
    topics = topic_match.extract_topics(text)
    if topics:
        hits = _backend_for_user(user_id).topic_match(user_id=user_id, topics=topics,
                                                      threshold=TOPIC_KEYWORD_THRESHOLD,
                                                      limit=TOPIC_INJECT_LIMIT)
        degraded = False
        # Cost-gated: only pay for the semantic pass when keyword matching
        # didn't already turn up a confident safety-tier hit -- unlike
        # preaction_check, an ordinary turn isn't the moment the operator's
        # refinement said must never be gated.
        if not any(h["safety_tier"] for h in hits):
            sem, degraded = semantic_safety_matches(user_id, text, limit=3)
            seen = {h["id"] for h in hits}
            hits.extend(m for m in sem if m["id"] not in seen)
        if hits or degraded:
            hits.sort(key=lambda h: (not h["safety_tier"], -h["score"]))
            lines = _format_matches(hits[:TOPIC_INJECT_LIMIT])
            if lines:
                prefix = "Relevant memory for this message"
                if degraded:
                    prefix += " (safety-tier semantic check degraded to keyword-only -- embedding provider unreachable)"
                mem_block = prefix + ":\n" + "\n".join(f"- {ln}" for ln in lines)
            elif degraded:
                mem_block = ("Memory topic-match degraded: couldn't reach the embedding provider to "
                            "check the safety/constraint tier this turn -- treat that tier as "
                            "unchecked, not clear.")

    notes_block = None
    notes, notes_degraded = nodrya_broad_search(session["workspace_id"], text, limit=3)
    note_lines = _format_note_matches(notes)
    if note_lines:
        note_prefix = "Possibly relevant notes from the operator's Nodrya account (not her own memory)"
        if notes_degraded:
            note_prefix += " -- Nodrya search degraded this turn"
        notes_block = note_prefix + ":\n" + "\n".join(f"- {ln}" for ln in note_lines)
    elif notes_degraded:
        notes_block = ("Broad Nodrya search degraded this turn -- couldn't reach Nodrya to check "
                       "for related notes.")

    blocks = [b for b in (mem_block, notes_block) if b]
    return "\n\n".join(blocks) if blocks else None


def preaction_check(session: dict, tool_name: str, args: dict) -> str | None:
    """Point 6's own pre-action hook -- called from chat.py's tool loop
    right after a consequential tool's own dispatch() call returns (see
    tools.py's Tool.consequential), using the ACTION's own name/arguments
    as the topic source rather than the message that started the turn --
    a consequential call decided several rounds into a turn has no fresh
    incoming message to re-extract from. Unlike topic_activation_line,
    the semantic pass over the safety tier is UNCONDITIONAL here, never
    gated on keyword confidence: the operator's own refinement was that
    confident-and-wrong keyword matching would otherwise hide the exact
    memory this exists to surface, right when it matters most.

    Always prints a line, on every branch, including the empty-result
    one -- so this hook running-and-finding-nothing is distinguishable in
    the logs from this hook never having been wired up at all, the same
    failure shape as the incident that motivated the compulsion-queue fix.

    Also runs nodrya_broad_search (2026-10-02, operator's own ask) over
    the same text, independent of the memory check above -- one more
    live call when enabled, same opt-in/best-effort posture as
    everywhere else this is wired in."""
    user_id = session["user_id"]
    text = f"{tool_name} " + " ".join(str(v) for v in (args or {}).values())
    topics = topic_match.extract_topics(text)
    backend = _backend_for(session["workspace_id"])
    hits = (backend.topic_match(user_id=user_id, topics=topics, threshold=TOPIC_KEYWORD_THRESHOLD,
                                limit=TOPIC_INJECT_LIMIT) if topics else [])
    sem, degraded = semantic_safety_matches(user_id, text, limit=3)
    seen = {h["id"] for h in hits}
    hits.extend(m for m in sem if m["id"] not in seen)
    print(f"memory.preaction_check: tool={tool_name} matches={len(hits)} degraded={degraded}",
         flush=True)

    mem_block = None
    if hits or degraded:
        hits.sort(key=lambda h: (not h["safety_tier"], -h["score"]))
        lines = _format_matches(hits[:TOPIC_INJECT_LIMIT])
        if lines:
            header = f"Before continuing past `{tool_name}`, memory relevant to that action"
            if degraded:
                header += " (safety-tier semantic check degraded to keyword-only -- embedding provider unreachable)"
            mem_block = header + ":\n" + "\n".join(f"- {ln}" for ln in lines)
        elif degraded:
            mem_block = (f"Memory check before `{tool_name}` degraded: couldn't reach the embedding "
                        f"provider to check the safety/constraint tier -- treat that tier as unchecked, "
                        f"not clear.")

    notes_block = None
    notes, notes_degraded = nodrya_broad_search(session["workspace_id"], text, limit=3)
    note_lines = _format_note_matches(notes)
    if note_lines:
        note_header = f"Possibly relevant Nodrya notes before `{tool_name}` (not her own memory)"
        if notes_degraded:
            note_header += " -- Nodrya search degraded this turn"
        notes_block = note_header + ":\n" + "\n".join(f"- {ln}" for ln in note_lines)
    elif notes_degraded:
        notes_block = (f"Broad Nodrya search before `{tool_name}` degraded this turn -- couldn't "
                       f"reach Nodrya to check for related notes.")

    blocks = [b for b in (mem_block, notes_block) if b]
    return "\n\n".join(blocks) if blocks else None


def _register_precheck() -> None:
    import precheck  # local: same reasoning as tools -- keeps precheck.py from needing to know this module exists
    precheck.register(pins_precheck_line)
    precheck.register(topic_activation_line)


_register_precheck()

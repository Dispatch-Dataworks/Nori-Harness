# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Pure topic-extraction/scoring math for memory.py's topic-triggered
activation (see that module's own docstring for the feature this serves).
Deliberately has zero dependency on store/config/network -- every function
here takes plain data in and returns plain data out, so it can be unit-
tested without a database or an API key, and so memory.py stays the only
module that touches the `memory` table (store.py's narrow-abstraction rule).

The scoring formula and threshold below aren't a guess -- they're the
exact ones measured against Nori's own real memory and message history
(2026-09-17): a naive keyword scorer recalled 2/7 real safety/constraint
memories that should have fired; adding stemming (this module's own
_stem, longest-suffix-first) raised that to 5/7. The one clean miss was a
genuine paraphrase with zero lexical overlap with its memory's tags or
content ("cage lock" vs a memory tagged only `household safety`) -- the
predicted failure mode, confirmed rather than assumed, and the reason
memory.py runs a semantic pass on top of this for the safety tier rather
than trusting keyword matching alone. See nori/docs/memory.md for the
full numbers and the honest "known weak spot" statement.
"""
from __future__ import annotations

import re

TAG_WEIGHT = 3.0
CONTENT_WEIGHT = 1.0

# Common English function words -- stripped before scoring so they can't
# inflate a content-hit count (nearly every message contains "the", "to",
# "a"...). Deliberately short and generic, not tuned to any one household;
# a false negative from an overly aggressive stopword list is worse than
# the mild noise of an occasional function word slipping through.
_STOPWORDS = frozenset("""
a an the this that these those is are was were be been being do does did
to of in on at for with from by as it its he she they them his her their
and or but if not no so than then there here what when where who whom
which how why can could should would will just also very really about
i you we me my your our us it's don't didn't can't won't isn't aren't
""".split())

# Checked longest-first so "meetings" and "meeting" both normalize to the
# same root ("meet") -- an earlier version of this checked shortest-first
# and got that pair wrong (meeting->meet, meetings->meeting), which showed
# up as a false miss during the real recall measurement. Crude on purpose
# (no real morphological analysis) -- this only needs to collapse a common
# inflection to its base often enough to help; it doesn't need to be right
# in general.
_SUFFIXES = ("ings", "ing", "es", "ed", "s")

_WORD_RE = re.compile(r"[a-z0-9']+")


def _stem(word: str) -> str:
    for suf in _SUFFIXES:
        if len(word) > len(suf) + 2 and word.endswith(suf):
            return word[: -len(suf)]
    return word


def extract_topics(text: str) -> list[str]:
    """Lowercase, tokenize, drop stopwords, stem -- the same normalization
    on both sides of a comparison (a memory's tags/content go through the
    identical path via score()) is what makes the comparison meaningful."""
    if not text:
        return []
    words = _WORD_RE.findall(text.lower())
    return [_stem(w) for w in words if w not in _STOPWORDS and len(w) > 1]


def score(topics: list[str], tags: list[str], content: str) -> tuple[float, int, int]:
    """(score, tag_hit_count, content_hit_count). tag hits are weighted
    higher than content hits -- an exact named entity or category the
    memory was explicitly tagged with is a stronger signal than the same
    word merely appearing somewhere in the fact's own text."""
    if not topics:
        return 0.0, 0, 0
    topic_set = set(topics)
    tag_stems = {_stem(t.lower()) for tag in (tags or []) for t in tag.split()}
    content_stems = set(extract_topics(content or ""))
    tag_hits = len(topic_set & tag_stems)
    content_hits = len(topic_set & content_stems)
    return TAG_WEIGHT * tag_hits + CONTENT_WEIGHT * content_hits, tag_hits, content_hits


def rank_keyword(topics: list[str], rows: list[dict], *, threshold: float, limit: int) -> list[dict]:
    """rows: memory dicts (id/type/value/tags/safety_tier/updated_ts).
    Returns matches at or above `threshold`, safety-tier rows first, then
    by score, then most-recently-updated -- capped to `limit`."""
    if not topics:
        return []
    scored = []
    for r in rows:
        s, tag_hits, content_hits = score(topics, r.get("tags") or [], r.get("value") or "")
        if s >= threshold:
            scored.append({"id": r["id"], "type": r["type"], "value": r["value"],
                          "safety_tier": bool(r.get("safety_tier")), "score": s,
                          "tag_hits": tag_hits, "content_hits": content_hits,
                          "updated_ts": r.get("updated_ts") or 0})
    scored.sort(key=lambda m: (not m["safety_tier"], -m["score"], -m["updated_ts"]))
    return scored[:limit]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

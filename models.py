# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Model roster -- the only module with raw SQL against `models`. Gives
the operator real control over which models Nori (and sub-agents) can
run on, without a code change or restart for anything but the currently-
selected default (see chat.py's own resolution logic).

The three models actually compared today (2026-09-12: Grok, gpt-4.1-mini,
gpt-5.6-luna) are seeded rows, not a hardcoded list -- once the operator
can add their own, the built-ins need to live in the same table or there
are two mechanisms doing the same job. `seed_defaults()` is idempotent
(checked by slug), safe to call every startup the same way mcp_servers.
register_all() already is.
"""
from __future__ import annotations

import time

import store

# (slug, label, reasoning_effort) -- reasoning_effort=None means "don't
# send the parameter"; both Grok and gpt-4.1-mini were tested without it
# and never needed it. Luna's is "low" -- the whole basis of the cost
# expectation for using it at all.
_SEEDED = [
    ("x-ai/grok-4.3", "Grok 4.3", None),
    ("openai/gpt-4.1-mini", "GPT-4.1 Mini", None),
    ("openai/gpt-5.6-luna", "GPT-5.6 Luna", "low"),
]

_VALID_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
# "none" deliberately NOT a selectable value here (2026-09-14, the models
# page's own confusing-pair bug) -- reasoning_effort=None already means
# "no reasoning" and is the only way to say that now. It used to also be
# expressible as the literal string "none", which looked like a second,
# different option in the dropdown ("(none -- do not send)" vs "none")
# even though both meant the same thing to every model except Luna --
# whose own native API rejects OMITTING the field outright when tools are
# present, needing the literal string sent instead. That's not something
# an admin should have to know or choose per model: chat.py's own
# per-model quirk table (_REQUIRES_LITERAL_NONE) now decides HOW "no
# reasoning" gets represented on the wire; the admin only ever picks
# whether reasoning is wanted, once, the same way for every model.


def seed_defaults() -> int:
    """Insert the tested trio if they aren't already there -- never
    overwrites an operator's own edits to them (an existing row, however
    it got there, is left alone)."""
    added = 0
    now = time.time()
    for slug, label, effort in _SEEDED:
        exists = store.read(lambda c, slug=slug: c.execute(
            "SELECT 1 FROM models WHERE slug=?", (slug,)).fetchone())
        if exists:
            continue
        store.write(lambda c, slug=slug, label=label, effort=effort: c.execute(
            "INSERT INTO models(slug, label, reasoning_effort, enabled, seeded, created_ts) "
            "VALUES (?,?,?,1,1,?)", (slug, label, effort, now)))
        added += 1
    return added


def list_all() -> list[dict]:
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT * FROM models ORDER BY seeded DESC, label").fetchall())]


def list_enabled() -> list[dict]:
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT * FROM models WHERE enabled=1 ORDER BY seeded DESC, label").fetchall())]


def get(model_id: int) -> dict | None:
    r = store.read(lambda c: c.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone())
    return dict(r) if r else None


def get_by_slug(slug: str) -> dict | None:
    r = store.read(lambda c: c.execute("SELECT * FROM models WHERE slug=?", (slug,)).fetchone())
    return dict(r) if r else None


def create(slug: str, label: str, reasoning_effort: str | None) -> tuple[bool, str]:
    slug = (slug or "").strip()
    label = (label or "").strip()
    reasoning_effort = (reasoning_effort or "").strip() or None
    if not slug or not label:
        return False, "slug and label are both required"
    if reasoning_effort and reasoning_effort not in _VALID_EFFORTS:
        return False, f"reasoning effort must be one of: {', '.join(_VALID_EFFORTS)}"
    if get_by_slug(slug) is not None:
        return False, f"{slug!r} is already in the list"
    store.write(lambda c: c.execute(
        "INSERT INTO models(slug, label, reasoning_effort, enabled, seeded, created_ts) "
        "VALUES (?,?,?,1,0,?)", (slug, label, reasoning_effort, time.time())))
    return True, "added"


def set_enabled(model_id: int, enabled: bool) -> None:
    store.write(lambda c: c.execute(
        "UPDATE models SET enabled=? WHERE id=?", (1 if enabled else 0, model_id)))


def set_reasoning_effort(model_id: int, reasoning_effort: str | None) -> tuple[bool, str]:
    reasoning_effort = (reasoning_effort or "").strip() or None
    if reasoning_effort and reasoning_effort not in _VALID_EFFORTS:
        return False, f"reasoning effort must be one of: {', '.join(_VALID_EFFORTS)}"
    store.write(lambda c: c.execute(
        "UPDATE models SET reasoning_effort=? WHERE id=?", (reasoning_effort, model_id)))
    return True, "saved"

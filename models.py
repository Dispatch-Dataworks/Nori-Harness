# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Model roster -- the only module with raw SQL against `models` and
`model_chain`. Reworked 2026-09-30 (see providers.py): a model is now an
alias + the provider it runs through + that provider's own model-name
string, not a flat OpenRouter-slug list. No more auto-seeded "tested
trio" -- those were implicitly OpenRouter-only, which doesn't generalize
now that a model can belong to any provider type; an admin adds their own
from the roster of providers they've actually configured, and there is
deliberately no default.
"""
from __future__ import annotations

import time

import store

_VALID_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
# "none" deliberately NOT a selectable value here (2026-09-14, the models
# page's own confusing-pair bug) -- reasoning_effort=None already means
# "no reasoning" and is the only way to say that now. See chat.py's own
# per-model quirk table for how "no reasoning" gets represented on the
# wire for a model that rejects omitting the field outright.


def list_all() -> list[dict]:
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT * FROM models ORDER BY alias").fetchall())]


def list_enabled() -> list[dict]:
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT * FROM models WHERE enabled=1 ORDER BY alias").fetchall())]


def get(model_id: int) -> dict | None:
    r = store.read(lambda c: c.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone())
    return dict(r) if r else None


def get_with_provider(model_id: int) -> dict | None:
    """A model row with its provider row nested under "provider" -- what
    chat.py's dispatch (call_for_model/call_via_chain) and jobs.py's
    sub-agent execution both actually need to make a call. None if the
    model doesn't exist, is disabled, or has no provider linked yet."""
    import providers
    row = get(model_id)
    if row is None or not row["enabled"] or not row["provider_id"]:
        return None
    provider = providers.get(row["provider_id"])
    if provider is None or not provider["enabled"]:
        return None
    row["provider"] = provider
    return row


def create(alias: str, provider_id: int, model_name: str, reasoning_effort: str | None) -> tuple[bool, str]:
    alias = (alias or "").strip()
    model_name = (model_name or "").strip()
    reasoning_effort = (reasoning_effort or "").strip() or None
    if not alias:
        return False, "an alias is required"
    if not provider_id:
        return False, "a provider is required"
    if not model_name:
        return False, "a model name is required"
    if reasoning_effort and reasoning_effort not in _VALID_EFFORTS:
        return False, f"reasoning effort must be one of: {', '.join(_VALID_EFFORTS)}"
    store.write(lambda c: c.execute(
        "INSERT INTO models(provider_id, model_name, alias, reasoning_effort, enabled, seeded, created_ts) "
        "VALUES (?,?,?,?,1,0,?)", (provider_id, model_name, alias, reasoning_effort, time.time())))
    return True, "added"


def delete(model_id: int) -> None:
    """References cleared BEFORE the models row itself -- both model_chain
    and sub_agents.model_id are real FKs to models(id) (PRAGMA
    foreign_keys=ON), so deleting the parent row first violates the
    constraint the moment either table still points at it (found live,
    2026-09-30: any migrated model already set as primary/fallback, or
    already assigned to a sub-agent, failed to delete). A sub-agent
    losing its model this way is left with model_id NULL -- "needs
    reconfiguring," surfaced honestly, not a crash."""
    store.write(lambda c: c.execute("DELETE FROM model_chain WHERE model_id=?", (model_id,)))
    store.write(lambda c: c.execute("UPDATE sub_agents SET model_id=NULL WHERE model_id=?", (model_id,)))
    store.write(lambda c: c.execute("DELETE FROM models WHERE id=?", (model_id,)))


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


# ── Primary + fallback chain (2026-09-30, replaces config.default_model_slug) ──

def get_chain(workspace_id: int) -> list[dict]:
    """Every model in this workspace's primary/fallback chain, in order
    (priority 0 = primary), each with its provider nested (see
    get_with_provider) -- entries whose model/provider has since been
    disabled or unlinked are silently dropped rather than raising, so a
    stale chain degrades to "try the next one" instead of erroring."""
    rows = store.read(lambda c: c.execute(
        "SELECT model_id FROM model_chain WHERE workspace_id=? ORDER BY priority", (workspace_id,)).fetchall())
    out = []
    for r in rows:
        entry = get_with_provider(r["model_id"])
        if entry is not None:
            out.append(entry)
    return out


def set_chain(workspace_id: int, model_ids: list[int]) -> None:
    """Replaces the whole chain for this workspace in one transaction --
    model_ids[0] becomes priority 0 (primary), the rest fallbacks in the
    order given. An empty list clears it entirely (no model configured)."""
    def _txn(c):
        c.execute("DELETE FROM model_chain WHERE workspace_id=?", (workspace_id,))
        for priority, model_id in enumerate(model_ids):
            c.execute("INSERT INTO model_chain(workspace_id, model_id, priority) VALUES (?,?,?)",
                     (workspace_id, model_id, priority))
    store.write(_txn)

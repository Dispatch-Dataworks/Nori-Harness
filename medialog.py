# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Audit trail + live spend accounting for Nori's image generation --
workspace-scoped counterpart to a sibling application's medialog.py (retyped, not
imported, same discipline as everywhere else between these two apps).

Spend is never a maintained running counter (a sibling application's own
runtime.img_spend_today/img_spend_total + its day-rollover logic) --
computed live from media_log rows instead, same pattern
jobs.cost_summary() already uses here, so there's no rollover logic to
get wrong and no risk of the counter drifting from the log that's
supposed to explain it.
"""
from __future__ import annotations

import time

import store
import usertime


def log_image_gen(*, workspace_id: int, user_id: int, purpose: str, prompt: str | None,
                  final_prompt: str | None, model: str | None, seed: int | None,
                  used_reference: bool | None, ok: bool, error: str | None = None,
                  cost_usd: float | None = None, cost_is_actual: bool | None = None,
                  file_id: str | None = None, caption: str | None = None) -> None:
    """purpose: 'generate_image_selfie' | 'imagine_image'. `prompt` is what
    she actually wrote; `final_prompt` is imagegen.assemble_prompt()'s
    output (style prefix + content constraint applied on top) -- both
    logged so a run of these over time shows whether the prefix is
    steering her prompts or fighting them, same reasoning as a sibling application's
    own version of this function."""
    store.write(lambda c: c.execute(
        "INSERT INTO media_log(workspace_id, user_id, ts, kind, purpose, prompt, final_prompt, "
        "model, seed, used_reference, ok, error, cost_usd, cost_is_actual, file_id, caption) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (workspace_id, user_id, time.time(), "image_gen", purpose,
         (prompt or None) and str(prompt)[:1000], (final_prompt or None) and str(final_prompt)[:1500],
         model, seed, None if used_reference is None else (1 if used_reference else 0),
         1 if ok else 0, (error or None) and str(error)[:500], cost_usd,
         None if cost_is_actual is None else (1 if cost_is_actual else 0), file_id,
         (caption or None) and str(caption)[:500])))


def log_chat_upload(*, workspace_id: int, user_id: int, file_id: str) -> None:
    """A casual chat photo the user sent, NOT a generation (2026-09-15) --
    a distinct kind/purpose so it's honestly distinguishable from
    generate_image_selfie/imagine_image in the recent-activity log,
    rather than reusing log_image_gen's shape for something that isn't
    one. cost_usd stays NULL -- spend_status()'s SUM() skips NULLs, so
    this can never inflate the household image budget. The real reason
    this exists: server.py's image_get route authorizes serving a file
    by checking media_log for a matching, ok=1 row scoped to this
    workspace -- a chat upload needs exactly that same row to be
    servable at all, same mechanism a generated image already relies on,
    not a second authorization path to keep in sync."""
    store.write(lambda c: c.execute(
        "INSERT INTO media_log(workspace_id, user_id, ts, kind, purpose, ok, file_id) "
        "VALUES (?,?,?,?,?,?,?)",
        (workspace_id, user_id, time.time(), "chat_upload", "chat_upload", 1, file_id)))


def spend_status(workspace_id: int, user_id: int) -> dict:
    """Live-computed spend for the shared household image budget -- both
    generate_image_selfie and imagine_image draw from this ONE total
    (operator's own decision: "one image budget"), so this is the single
    place either tool checks before spending, and the single place
    /admin/media shows the household what's left.

    `user_id` decides whose zone "today" resets in (2026-09-25, the
    operator: "fix budgets to all use timezones" -- this used to be
    time.time() % 86400, always UTC-midnight regardless of where the
    household actually is). The budget itself stays shared/workspace-wide;
    only the day boundary is per-user, since there is no single "the
    workspace's timezone" to fall back on -- the acting/viewing user's own
    configured zone (usertime.py) is the caller's real "today" either way,
    generate_image_selfie/imagine_image's own caller and the settings
    page's own viewer alike."""
    import config
    since_today = usertime.day_start(user_id)
    row_today = store.read(lambda c: c.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM media_log "
        "WHERE workspace_id=? AND ts>=? AND ok=1", (workspace_id, since_today)).fetchone())
    row_total = store.read(lambda c: c.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM media_log "
        "WHERE workspace_id=? AND ok=1", (workspace_id,)).fetchone())
    cap = config.get("workspace", workspace_id, "image_daily_cap_usd")
    spent_today = round(row_today["total"] or 0.0, 4)
    return {
        "enabled": config.get("workspace", workspace_id, "image_gen_enabled"),
        "model": config.get("workspace", workspace_id, "image_model"),
        "per_request_usd": config.get("workspace", workspace_id, "image_cost_per_request_usd"),
        "spent_today_usd": spent_today,
        "spent_total_usd": round(row_total["total"] or 0.0, 4),
        "daily_cap_usd": cap,
        "remaining_today_usd": round(max(0.0, cap - spent_today), 4),
    }


def recent_log(workspace_id: int, limit: int = 20) -> list[dict]:
    """Newest-first, for the /admin/media panel -- same "admin sees a
    bounded recent slice, older rows still reachable in the raw table"
    shape as peer_debug_limit's own reasoning."""
    rows = store.read(lambda c: c.execute(
        "SELECT * FROM media_log WHERE workspace_id=? ORDER BY ts DESC LIMIT ?",
        (workspace_id, limit)).fetchall())
    return [dict(r) for r in rows]

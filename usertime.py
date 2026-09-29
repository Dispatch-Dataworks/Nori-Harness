# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Single source of truth for "what timezone is he in," and every
wall-clock <-> instant conversion that needs it (2026-09-18, real bug
found twice: the calendar tool and the water tracker both misbehaved
because nothing in this app had an explicit, configured timezone --
every site that needed local time called time.localtime()/mktime(),
which implicitly trusts the SERVER's own OS clock. Correct for him only
by coincidence of where this happens to run.

Storage stays UTC epoch (time.time()) everywhere -- unchanged, and
correct; nothing here touches that. This module is only about
BOUNDARIES (today, this week, a day's start for aggregation) and
RENDERING (what to show him) -- the two places a naive local-time call
actually goes wrong.

Every function takes user_id explicitly and re-reads config on every
call (config.get() is already a cheap indexed SELECT, same cost every
other per-call config lookup in this app already pays) -- no caching,
so a settings-page change takes effect on the very next call, not after
a restart.
"""
from __future__ import annotations

import datetime

import config

_FALLBACK_TZ = "America/New_York"


def zone_name(user_id: int) -> str:
    return config.get("user", user_id, "timezone") or _FALLBACK_TZ


def zone_for(user_id: int) -> datetime.tzinfo:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(zone_name(user_id))
    except ZoneInfoNotFoundError:
        return ZoneInfo(_FALLBACK_TZ)


def is_valid_zone(name: str) -> bool:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def local_dt(user_id: int, ts: float | None = None) -> datetime.datetime:
    """The time.localtime(ts) replacement -- an aware datetime in HIS
    configured zone, not the server's OS zone."""
    import time
    dt = datetime.datetime.fromtimestamp(ts if ts is not None else time.time(), tz=datetime.timezone.utc)
    return dt.astimezone(zone_for(user_id))


def to_epoch(user_id: int, year: int, month: int, day: int, hour: int = 0,
            minute: int = 0, second: int = 0) -> float:
    """The time.mktime(...) replacement -- builds the real instant for
    these WALL-CLOCK components in his zone. DST-correct by
    construction: zoneinfo resolves the fold/gap for this exact date
    itself, rather than the caller adding raw seconds and hoping the
    offset didn't change in between (recurrence.py's own real bug,
    fixed the same day this module was written)."""
    return datetime.datetime(year, month, day, hour, minute, second, tzinfo=zone_for(user_id)).timestamp()


def day_start(user_id: int, ts: float | None = None) -> float:
    """Start of the calendar day `ts` (default: now) falls in, IN HIS
    ZONE -- the boundary "today"/"this week" aggregation needs, and the
    one the water tracker bug was really about (tasks.parse_when's
    "today" resolves to end-of-day, correct for a due-date ceiling,
    wrong as a lower bound; this is the real lower-bound primitive)."""
    lt = local_dt(user_id, ts)
    return to_epoch(user_id, lt.year, lt.month, lt.day, 0, 0, 0)


def day_end(user_id: int, ts: float | None = None) -> float:
    lt = local_dt(user_id, ts)
    return to_epoch(user_id, lt.year, lt.month, lt.day, 23, 59, 59)


def week_start(user_id: int, ts: float | None = None) -> float:
    """Monday 00:00 of the week `ts` falls in -- same 0=Monday convention
    recurrence.py/time.localtime().tm_wday already use everywhere else."""
    lt = local_dt(user_id, ts)
    monday = lt.date() - datetime.timedelta(days=lt.weekday())
    return to_epoch(user_id, monday.year, monday.month, monday.day, 0, 0, 0)


def fmt(user_id: int, ts: float, pattern: str = "%Y-%m-%d %H:%M") -> str:
    """The time.strftime(pattern, time.localtime(ts)) replacement."""
    if not ts:
        return ""
    return local_dt(user_id, ts).strftime(pattern)


def maybe_set_from_calendar(user_id: int, calendar_tz_name: str) -> None:
    """Called once, right after a Google Calendar connects (server.py's
    oauth_callback_get) -- auto-fills the timezone setting from the
    calendar's own reported zone, but ONLY if he's never touched this
    setting himself. Detected by comparing against the untouched
    default rather than a separate "has he set this" flag -- simplest
    thing that's still correct: the one case this could wrongly
    overwrite a REAL, deliberate choice of America/New_York is if he
    explicitly chose that exact value himself, which isn't a real loss
    since it's already what's stored."""
    if not calendar_tz_name or not is_valid_zone(calendar_tz_name):
        return
    current = config.get("user", user_id, "timezone")
    default = config.spec()["timezone"][0]
    if current == default:
        config.set("user", user_id, "timezone", calendar_tz_name)

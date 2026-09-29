# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Shared recurrence math (2026-09-16) -- with the same catch-up-safe
policy schedules.py needed first (2026-09-15): a recurrence's next
occurrence is always computed relative to `after` (the real current/
completion time), never the missed slot, so a process that was down, or
a task closed late, doesn't replay a backlog -- it just advances forward
from now.

Extracted out of schedules.py (2026-09-16, operator's own explicit
instruction: "you just built schedule recurrence for the scheduler,
reuse that rather than a second implementation") the moment a SECOND
thing -- tasks.py's own due-date recurrence -- needed the identical rule
shape and the identical advance-forward-from-now policy. Neither module
owns this; both import it. schedules.py keeps its own public names
(SCHEDULE_TYPES, MIN_INTERVAL_MIN, MAX_INTERVAL_MIN) as re-exports of
this module's, so nothing that already reads those breaks.

Extended the same day, for reminders.py, with 'weekly'/'monthly_day'/
'monthly_weekday' -- schedules' own two types ('interval', a fixed
minutes cadence; 'time', daily at a fixed clock time) never needed
anything narrower than a day, but "weekly on a day" and "monthly on the
Nth day or Nth weekday" are the operator's own explicit reminder
patterns and don't fit either existing shape. reminders.py never uses
'interval' (a reminder due "every 30 minutes" was never a stated need),
but nothing here stops it from being available if some future caller
wants it -- the type list is shared infrastructure, not gatekept per
caller.

`weekday` throughout is 0=Monday..6=Sunday (Python's own date.weekday()
convention, and calendar's) -- one convention, not translated per
caller. `week_ordinal` for 'monthly_weekday' is 1-4 for the 1st-4th
occurrence, or -1 for "last" -- deliberately never a literal "5th":
every month has AT LEAST four of any given weekday (a month is always
>=28 days), so 1-4 always exist, but a 5th only exists some months.
"Last Friday" is exactly `week_ordinal=-1`, whether that lands on the
month's 4th or a real 5th Friday.

── Timezone (2026-09-18, real bug fixed) ─────────────────────────────
Every "next occurrence" is now computed against an explicit `tz`
(usertime.zone_for(user_id)), never the server's own OS clock the way
time.localtime()/mktime() implicitly did before this. mk() builds the
real instant for given WALL-CLOCK components via zoneinfo, which
resolves that date's own DST fold/gap correctly by construction --
the actual fix is in compute_next()'s "roll forward to the next
candidate" step for 'time' and 'weekly': the old code advanced by
adding a flat 24*3600 or N*86400 SECONDS to an already-resolved epoch,
which drifts an hour across a DST transition (86400 real seconds after
8am EDT is NOT 8am the next day once the clock has moved). Fixed to
advance the CALENDAR DATE (datetime.date + timedelta(days=N), which has
no time-of-day and so nothing to drift) and re-resolve the wall-clock
time for that new date via mk() -- the exact place a decades-old class
of recurring-scheduler bug usually hides, found here by checking DST
transitions directly rather than trusting the arithmetic looked right.
"""
from __future__ import annotations

import calendar
import datetime

TYPES = ("interval", "time", "weekly", "monthly_day", "monthly_weekday")
WEEK_ORDINALS = (1, 2, 3, 4, -1)

# A floor, not a real limit -- keeps a mistyped interval_min=1 from turning
# into a tight loop of real model turns (and real cost) rather than the
# once-every-few-minutes cadence anything legitimate actually needs.
MIN_INTERVAL_MIN = 5
MAX_INTERVAL_MIN = 60 * 24 * 30  # 30 days -- generous, not a real constraint


def _needs_time(recur_type: str, time_hour, time_minute) -> str | None:
    if (time_hour is None or time_minute is None
            or not (0 <= time_hour <= 23) or not (0 <= time_minute <= 59)):
        return "time_hour/time_minute must be a valid 24-hour time"
    return None


def validate(recur_type: str | None, interval_min: int | None = None, time_hour: int | None = None,
            time_minute: int | None = None, *, weekday: int | None = None,
            month_day: int | None = None, week_ordinal: int | None = None) -> str | None:
    """None for recur_type means "no recurrence" -- always valid, every
    other field is simply ignored. Anything else must be one of TYPES
    with the fields THAT type actually needs -- the rest stay ignored
    too (e.g. weekday is irrelevant to 'time', month_day irrelevant to
    'weekly'), same shape schedules.py's own interval-vs-time check
    already used before this got extended."""
    if recur_type is None:
        return None
    if recur_type not in TYPES:
        return f"recur_type must be one of: {', '.join(TYPES)}"
    if recur_type == "interval":
        if not interval_min or not (MIN_INTERVAL_MIN <= interval_min <= MAX_INTERVAL_MIN):
            return f"interval_min must be between {MIN_INTERVAL_MIN} and {MAX_INTERVAL_MIN}"
        return None
    if recur_type == "time":
        return _needs_time(recur_type, time_hour, time_minute)
    if recur_type == "weekly":
        err = _needs_time(recur_type, time_hour, time_minute)
        if err:
            return err
        if weekday is None or not (0 <= weekday <= 6):
            return "weekday must be 0 (Monday) through 6 (Sunday)"
        return None
    if recur_type == "monthly_day":
        err = _needs_time(recur_type, time_hour, time_minute)
        if err:
            return err
        if month_day is None or not (1 <= month_day <= 31):
            return "month_day must be between 1 and 31"
        return None
    if recur_type == "monthly_weekday":
        err = _needs_time(recur_type, time_hour, time_minute)
        if err:
            return err
        if weekday is None or not (0 <= weekday <= 6):
            return "weekday must be 0 (Monday) through 6 (Sunday)"
        if week_ordinal is None or week_ordinal not in WEEK_ORDINALS:
            return f"week_ordinal must be one of: {', '.join(str(w) for w in WEEK_ORDINALS)} (-1 = last)"
        return None
    return None


def mk(tz, year: int, month: int, day: int, hour: int, minute: int) -> float:
    """The real instant for these wall-clock components in `tz`. DST-
    correct by construction -- zoneinfo resolves this exact date's own
    fold/gap, not a guess based on today's offset."""
    return datetime.datetime(year, month, day, hour, minute, 0, tzinfo=tz).timestamp()


def _local_date(tz, ts: float) -> datetime.date:
    return datetime.datetime.fromtimestamp(ts, tz=tz).date()


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _nth_weekday_of_month(year: int, month: int, weekday: int, ordinal: int) -> int:
    """Day-of-month (1-31) of the ordinal-th occurrence of `weekday` in
    this month, or the LAST occurrence when ordinal == -1. See the
    module docstring for why "5th" is never a literal option here."""
    first_weekday = calendar.monthrange(year, month)[0]  # 0=Monday, day 1's own weekday
    days_in_month = calendar.monthrange(year, month)[1]
    first_occurrence = 1 + (weekday - first_weekday) % 7
    if ordinal == -1:
        day = first_occurrence
        while day + 7 <= days_in_month:
            day += 7
        return day
    return first_occurrence + (ordinal - 1) * 7  # always <= days_in_month for ordinal 1-4


def compute_next(recur_type: str, interval_min: int | None = None, time_hour: int | None = None,
                 time_minute: int | None = None, *, after: float, tz,
                 weekday: int | None = None, month_day: int | None = None,
                 week_ordinal: int | None = None) -> float:
    """The catch-up policy: always relative to `after`, never to whatever
    slot was missed -- see the module docstring. `tz` (usertime.zone_for
    (user_id)) is required for every type except 'interval', which is
    pure elapsed-seconds and has no wall-clock component to place in a
    zone at all."""
    if recur_type == "interval":
        return after + interval_min * 60
    if recur_type == "time":
        d = _local_date(tz, after)
        candidate = mk(tz, d.year, d.month, d.day, time_hour, time_minute)
        if candidate <= after:
            d += datetime.timedelta(days=1)
            candidate = mk(tz, d.year, d.month, d.day, time_hour, time_minute)
        return candidate
    if recur_type == "weekly":
        d = _local_date(tz, after)
        days_ahead = (weekday - d.weekday()) % 7
        cand_date = d + datetime.timedelta(days=days_ahead)
        candidate = mk(tz, cand_date.year, cand_date.month, cand_date.day, time_hour, time_minute)
        if candidate <= after:
            cand_date += datetime.timedelta(days=7)
            candidate = mk(tz, cand_date.year, cand_date.month, cand_date.day, time_hour, time_minute)
        return candidate
    if recur_type == "monthly_day":
        d = _local_date(tz, after)
        def _for(year, month):
            days_in_month = calendar.monthrange(year, month)[1]
            day = min(month_day, days_in_month)  # clamp -- e.g. the 31st in a 30-day month -> last day
            return mk(tz, year, month, day, time_hour, time_minute)
        candidate = _for(d.year, d.month)
        if candidate <= after:
            candidate = _for(*_next_month(d.year, d.month))
        return candidate
    if recur_type == "monthly_weekday":
        d = _local_date(tz, after)
        def _for(year, month):
            day = _nth_weekday_of_month(year, month, weekday, week_ordinal)
            return mk(tz, year, month, day, time_hour, time_minute)
        candidate = _for(d.year, d.month)
        if candidate <= after:
            candidate = _for(*_next_month(d.year, d.month))
        return candidate
    raise ValueError(f"unknown recur_type: {recur_type!r}")

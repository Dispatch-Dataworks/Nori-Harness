# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Real store, real config, real zoneinfo (2026-09-18) -- the timezone
audit's own two explicitly-required boundary properties: an entry near
midnight lands on the right calendar day, and a daily/weekly recurrence
fires at the same LOCAL hour on both sides of a real DST transition,
never drifting an hour. tzdata (PyPI) had to be installed on this
Windows box for zoneinfo to resolve any real IANA name at all;
without it every test below would raise
ZoneInfoNotFoundError, not silently pass.
"""
import datetime
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_usertime_")
os.environ["NORI_DATA_DIR"] = _SCRATCH

import accounts  # noqa: E402
import config  # noqa: E402
import recurrence  # noqa: E402
import store  # noqa: E402
import tasks  # noqa: E402
import usertime  # noqa: E402

_NY = ZoneInfo("America/New_York")
# Real, confirmed transitions for 2026 (found by scanning zoneinfo
# itself, not assumed from memory of the rule).
_SPRING_FORWARD = (2026, 3, 8)   # EST -> EDT
_FALL_BACK = (2026, 11, 1)       # EDT -> EST


class UserTimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init()
        user = accounts.bootstrap_admin("Tester", "testpass123")
        cls.user_id = user["id"]
        config.set("user", cls.user_id, "timezone", "America/New_York")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_SCRATCH, ignore_errors=True)

    def test_entry_at_11pm_local_counts_as_that_day_not_next(self):
        # 2026-09-16 23:00 America/New_York -- one hour before midnight.
        import datetime
        eleven_pm = usertime.to_epoch(self.user_id, 2026, 9, 16, 23, 0, 0)
        start, end = usertime.day_start(self.user_id, eleven_pm), usertime.day_end(self.user_id, eleven_pm)
        self.assertLessEqual(start, eleven_pm)
        self.assertLessEqual(eleven_pm, end)
        self.assertEqual(usertime.local_dt(self.user_id, start).day, 16)
        self.assertEqual(usertime.local_dt(self.user_id, end).day, 16)
        # And the NEXT day's start is a real 24h+ later, not the same day.
        next_day_start = usertime.day_start(self.user_id, eleven_pm + 3600 * 2)
        self.assertGreater(next_day_start, eleven_pm)
        self.assertEqual(usertime.local_dt(self.user_id, next_day_start).day, 17)

    def test_tracker_since_today_no_longer_inverts(self):
        # The real bug: parse_when("today") is 23:59 today, wrong as a
        # lower bound. parse_since("today") must be the START of today,
        # always <= "now" (whenever "now" is, today).
        since = tasks.parse_since("today", self.user_id)
        now = usertime.local_dt(self.user_id).timestamp()
        self.assertLessEqual(since, now)
        self.assertEqual(usertime.local_dt(self.user_id, since).hour, 0)

    def test_daily_recurrence_survives_spring_forward_dst(self):
        d = datetime.date(*_SPRING_FORWARD)
        prev = d - datetime.timedelta(days=1)
        before = usertime.to_epoch(self.user_id, prev.year, prev.month, prev.day, 8, 30, 0)
        first = recurrence.compute_next("time", time_hour=8, time_minute=0, after=before, tz=_NY)
        self.assertEqual(usertime.local_dt(self.user_id, first).hour, 8)
        self.assertEqual(usertime.local_dt(self.user_id, first).date(), d)
        second = recurrence.compute_next("time", time_hour=8, time_minute=0, after=first, tz=_NY)
        self.assertEqual(usertime.local_dt(self.user_id, second).hour, 8,
                         "8am fire drifted an hour crossing the spring-forward transition")
        self.assertEqual(usertime.local_dt(self.user_id, second).date(), d + datetime.timedelta(days=1))

    def test_daily_recurrence_survives_fall_back_dst(self):
        d = datetime.date(*_FALL_BACK)
        prev = d - datetime.timedelta(days=1)
        before = usertime.to_epoch(self.user_id, prev.year, prev.month, prev.day, 8, 30, 0)
        first = recurrence.compute_next("time", time_hour=8, time_minute=0, after=before, tz=_NY)
        self.assertEqual(usertime.local_dt(self.user_id, first).hour, 8)
        self.assertEqual(usertime.local_dt(self.user_id, first).date(), d)
        second = recurrence.compute_next("time", time_hour=8, time_minute=0, after=first, tz=_NY)
        self.assertEqual(usertime.local_dt(self.user_id, second).hour, 8,
                         "8am fire drifted an hour crossing the fall-back transition")
        self.assertEqual(usertime.local_dt(self.user_id, second).date(), d + datetime.timedelta(days=1))

    def test_weekly_recurrence_survives_dst(self):
        # A week that spans the fall-back transition (Nov 1, 2026) --
        # weekly's own "add days_ahead then re-mk" path is the one that
        # used to add raw epoch seconds across possibly-changing offsets.
        d = datetime.date(*_FALL_BACK)
        anchor = d - datetime.timedelta(days=3)
        anchor_weekday = anchor.weekday()
        after = usertime.to_epoch(self.user_id, anchor.year, anchor.month, anchor.day, 9, 0, 0)
        nxt = recurrence.compute_next("weekly", time_hour=9, time_minute=0, after=after, tz=_NY,
                                      weekday=anchor_weekday)
        # One week later than the anchor day, still 9am local, not 8 or 10.
        self.assertEqual(usertime.local_dt(self.user_id, nxt).hour, 9)
        self.assertEqual(usertime.local_dt(self.user_id, nxt).date(), anchor + datetime.timedelta(days=7))

    def test_mk_and_to_epoch_agree_across_a_dst_boundary(self):
        # Sanity: the same wall-clock time, day before and day after a
        # transition, must be exactly 23h or 25h apart in real seconds
        # (never a clean 24h), proving the conversion is DST-aware.
        d = datetime.date(*_FALL_BACK)
        prev = d - datetime.timedelta(days=1)
        before = recurrence.mk(_NY, prev.year, prev.month, prev.day, 8, 0)
        after = recurrence.mk(_NY, d.year, d.month, d.day, 8, 0)
        self.assertEqual(after - before, 25 * 3600)  # fall back -- the day gets a real extra hour


if __name__ == "__main__":
    unittest.main()

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Every daily budget/rollover reads "today" in the user's own configured
zone now, never raw UTC-epoch arithmetic (2026-09-25, the operator: "fix
budgets to all use timezones" -- found on medialog.spend_status's image
budget, audited and fixed the same way in conversation.cost_summary and
jobs.cost_summary too).

The proof, fully deterministic (a fixed reference instant, never live
"now" -- the relationship between a fixed-offset zone's own midnight and
UTC's flips at some point every single day, so a test anchored to live
time would itself be time-of-day-dependent): at a frozen 2026-01-15 12:00
UTC, a user in Etc/GMT+8 (a fixed UTC-8 offset, no DST to complicate the
arithmetic) has NOT yet reached their own local midnight -- that happens
at 2026-01-15 08:00 UTC, eight hours later than UTC's own midnight for
the same date. A row timestamped 2026-01-15 05:00 UTC is "today" by raw
UTC-epoch arithmetic (time.time() % 86400) but is still genuinely
YESTERDAY, 9pm local, for this user -- the old code would have wrongly
counted it into today's spend; the fix correctly leaves it out.
"""
import datetime
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_budget_tz_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import conversation  # noqa: E402
import jobs  # noqa: E402
import medialog  # noqa: E402
import store  # noqa: E402
import sub_agents  # noqa: E402
import usertime  # noqa: E402

store.init()

_ZONE = "Etc/GMT+8"   # a fixed UTC-8 offset, no DST -- keeps this test's own hand-computed arithmetic exact
_FIXED_NOW = datetime.datetime(2026, 1, 15, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
_UTC_MIDNIGHT = datetime.datetime(2026, 1, 15, 0, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
_LOCAL_MIDNIGHT_UTC = datetime.datetime(2026, 1, 15, 8, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
# Between the two boundaries: "today" by raw UTC-epoch arithmetic, but
# still yesterday (9pm local) for a UTC-8 user -- the exact disputed case.
_DISPUTED_TS = datetime.datetime(2026, 1, 15, 5, 0, 0, tzinfo=datetime.timezone.utc).timestamp()


class BudgetTimezoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.user = accounts.bootstrap_admin("Tester", "testpass123")
        cls.wsid = cls.user["workspace_id"]
        cls.uid = cls.user["id"]
        import config
        config.set("user", cls.uid, "timezone", _ZONE)

    def setUp(self):
        with patch("time.time", return_value=_FIXED_NOW):
            self.assertEqual(usertime.day_start(self.uid), _LOCAL_MIDNIGHT_UTC)   # confirms the test's own hand-computed arithmetic matches the real zone-aware function
            self.assertGreater(_LOCAL_MIDNIGHT_UTC, _UTC_MIDNIGHT)               # confirms the disputed window (between the two) actually exists at this instant

    def test_image_budget_excludes_the_disputed_hour_a_user_still_yesterday_would_not_expect_counted(self):
        medialog.log_image_gen(workspace_id=self.wsid, user_id=self.uid, purpose="imagine_image",
                               prompt="p", final_prompt="p", model="m", seed=1, used_reference=False,
                               ok=True, cost_usd=0.05)
        store.write(lambda c: c.execute(
            "UPDATE media_log SET ts=? WHERE id=(SELECT MAX(id) FROM media_log WHERE workspace_id=?)",
            (_DISPUTED_TS, self.wsid)))
        with patch("time.time", return_value=_FIXED_NOW):
            status = medialog.spend_status(self.wsid, self.uid)
        self.assertEqual(status["spent_today_usd"], 0.0,
                        "the old UTC-epoch boundary would have wrongly counted this as today's spend")
        self.assertEqual(status["spent_total_usd"], 0.05, "the row is real and still counted in the running total")

    def test_conversation_cost_summary_excludes_the_disputed_hour(self):
        store.write(lambda c: c.execute(
            "INSERT INTO messages(user_id, ts, role, kind, content, meta) VALUES (?,?,?,?,?,?)",
            (self.uid, _DISPUTED_TS, "assistant", "chat", "hi", '{"cost_usd": 0.02}')))
        with patch("time.time", return_value=_FIXED_NOW):
            summary = conversation.cost_summary(self.uid)
        self.assertEqual(summary["today"]["cost"], 0.0)
        self.assertAlmostEqual(summary["period"]["cost"], 0.02, places=6)

    def test_jobs_cost_summary_excludes_the_disputed_hour(self):
        ok, sid = sub_agents.create(self.uid, "Tester Agent", "x-ai/grok-4.3",
                                    "https://openrouter.ai/api/v1", "")
        self.assertTrue(ok, sid)
        store.write(lambda c: c.execute(
            "INSERT INTO jobs(user_id, sub_agent_id, task, status, created_ts, cost_usd) "
            "VALUES (?,?,?,?,?,?)", (self.uid, sid, "a task", "done", _DISPUTED_TS, 0.03)))
        with patch("time.time", return_value=_FIXED_NOW):
            summary = jobs.cost_summary(self.uid)
        self.assertEqual(summary["today"]["cost"], 0.0)
        self.assertAlmostEqual(summary["period"]["cost"], 0.03, places=6)


if __name__ == "__main__":
    unittest.main()

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""_TimestampedWriter (2026-09-17, real incident, same-day crash-loop:
.nori.err (and a sibling application's own equivalent log) carried no timestamps at all, which is what
actually blocked diagnosing it). Covers the two
real write patterns that matter: a plain print() (one write() call per
line) and a multi-line traceback dump (several write() calls that don't
line up with real line boundaries) -- both need every real line stamped
exactly once, not split or merged.
"""
import io
import os
import sys
import tempfile
import traceback
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# server.py transitively imports store.py, which refuses to pick a data
# directory silently -- same live-data guard every other test here sets.
os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_timestamped_log_")
os.environ["NORI_NO_LOGFILE"] = "1"

import server as nori_server  # noqa: E402


class TimestampedWriter(unittest.TestCase):
    def test_plain_print_gets_one_stamp_per_line(self):
        buf = io.StringIO()
        w = nori_server._TimestampedWriter(buf)
        print("first line", file=w)
        print("second line", file=w)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        for line, text in zip(lines, ("first line", "second line")):
            self.assertTrue(line.endswith(text), line)
            stamp = line[: -len(text)].strip()
            self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

    def test_multiwrite_traceback_gets_one_stamp_per_real_line_not_per_write_call(self):
        buf = io.StringIO()
        w = nori_server._TimestampedWriter(buf)
        try:
            raise ValueError("boom")
        except ValueError:
            traceback.print_exc(file=w)
        out = buf.getvalue()
        lines = out.splitlines()
        # Every real line (however many separate write() calls produced
        # it) has its own single leading stamp -- not zero, not doubled
        # mid-line, and the traceback's own text survives intact.
        for line in lines:
            self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} ")
        self.assertIn("ValueError: boom", out)
        self.assertIn("Traceback (most recent call last):", out)

    def test_unterminated_trailing_text_is_stamped_on_flush(self):
        buf = io.StringIO()
        w = nori_server._TimestampedWriter(buf)
        w.write("no newline yet")
        self.assertEqual(buf.getvalue(), "")  # buffered, not lost, not stamped early
        w.flush()
        self.assertRegex(buf.getvalue(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} no newline yet$")


if __name__ == "__main__":
    unittest.main()

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Message times in the chat (2026-10-24): a small clock under each message and a day label wherever the date changes, drawn in the user's own
configured timezone -- on page load (server.py's _bubble) and for messages that arrive live (the poll/send responses carry `ts`; CHAT_JS formats
it with the zone the page is given). Real server code, scratch database."""
import datetime
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_chat_times_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_chat_times_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import config  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Tester", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
SESS = {"user_id": UID, "workspace_id": WS, "role": "admin", "csrf": "csrf-tok"}
NY = datetime.timezone(datetime.timedelta(hours=-4))        # EDT in October


def _ts(y, mo, d, h, mi):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=NY).timestamp()


def _add(ts, role, content, kind="chat"):
    store.write(lambda c: c.execute("INSERT INTO messages(user_id, ts, role, content, kind) VALUES (?,?,?,?,?)", (UID, ts, role, content, kind)))


class _H(server.Handler):
    def __init__(self):
        self.headers = {}
        self.sent = []

    def send(self, code, body=b"", extra=None, *, ctype="text/html; charset=utf-8"):
        self.sent.append(body)

    def ip(self):
        return "10.0.0.1"


def _page():
    h = _H()
    h.chat_page(dict(SESS))
    body = h.sent[-1]
    return body.decode("utf-8") if isinstance(body, bytes) else body


class PageLoad(unittest.TestCase):
    def setUp(self):
        store.write(lambda c: c.execute("DELETE FROM messages"))
        config.set("user", UID, "timezone", "America/New_York")

    def test_each_message_has_its_time_in_the_users_zone_and_a_full_date_on_hover(self):
        # 20:34 UTC is 4:34 PM in New York -- the page must say 4:34 PM, never the server's UTC 8:34 PM
        ts = datetime.datetime(2026, 10, 21, 20, 34, tzinfo=datetime.timezone.utc).timestamp()
        _add(ts, "user", "What time is it?")
        html = _page()
        self.assertIn(">4:34 PM</time>", html)
        self.assertNotIn("8:34 PM", html)
        self.assertIn("title='Wednesday, Oct 21, 2026 04:34 PM'", html)
        self.assertIn("datetime='2026-10-21T16:34:00'", html)

    def test_it_follows_the_zone_setting(self):
        ts = datetime.datetime(2026, 10, 21, 20, 34, tzinfo=datetime.timezone.utc).timestamp()
        _add(ts, "assistant", "Hi")
        config.set("user", UID, "timezone", "Asia/Tokyo")
        html = _page()
        self.assertIn(">5:34 AM</time>", html)
        self.assertIn('"tz": "Asia/Tokyo"', html)

    def test_a_day_label_appears_when_the_date_changes_and_not_otherwise(self):
        now = datetime.datetime.now(NY)
        two_days_ago = (now - datetime.timedelta(days=2)).replace(hour=9, minute=5)
        _add(two_days_ago.timestamp(), "user", "first")
        _add((two_days_ago + datetime.timedelta(minutes=3)).timestamp(), "assistant", "same day")
        _add(now.timestamp(), "user", "today")
        html = _page()
        labels = re.findall(r"<div class=daysep>([^<]*)</div>", html)
        self.assertEqual(len(labels), 2, labels)
        self.assertRegex(labels[0], r"^[A-Z][a-z]+day, [A-Z][a-z]{2} \d{1,2}(, \d{4})?$")
        self.assertEqual(labels[1], "Today")
        self.assertLess(html.index("first"), html.index("same day"))
        self.assertEqual(html.count("<div class=daysep>"), 2, "not between two messages on the same day")

    def test_yesterday_and_a_previous_year(self):
        now = datetime.datetime.now(NY)
        _add((now - datetime.timedelta(days=1)).timestamp(), "user", "a")
        _add((now - datetime.timedelta(days=400)).timestamp() - 86400 * 5, "user", "old")
        html = _page()
        labels = re.findall(r"<div class=daysep>([^<]*)</div>", html)
        self.assertIn("Yesterday", labels)
        self.assertTrue(any(re.search(r", \d{4}$", lab) for lab in labels), f"an older year names the year: {labels}")

    def test_the_label_for_midnight_uses_their_day_not_the_servers(self):
        # 03:30 UTC on the 22nd is 11:30 PM on the 21st in New York: same day as a 4:34 PM message on the 21st
        a = datetime.datetime(2026, 10, 21, 20, 34, tzinfo=datetime.timezone.utc).timestamp()
        b = datetime.datetime(2026, 10, 22, 3, 30, tzinfo=datetime.timezone.utc).timestamp()
        _add(a, "user", "evening")
        _add(b, "assistant", "late")
        self.assertEqual(_page().count("<div class=daysep>"), 1)

    def test_tool_lines_and_image_bubbles_are_handled(self):
        ts = _ts(2026, 10, 21, 9, 0)
        _add(ts, "assistant", "used recall", kind="tool")
        _add(ts + 5, "assistant", "a picture", kind="image")
        store.write(lambda c: c.execute("UPDATE messages SET meta=? WHERE kind='image'", (json.dumps({"file_id": "abc"}),)))
        html = _page()
        self.assertIn("class=toolline", html)
        self.assertIn(">9:00 AM</time>", html)


class LiveFeed(unittest.TestCase):
    def test_the_poll_response_carries_each_messages_timestamp(self):
        store.write(lambda c: c.execute("DELETE FROM messages"))
        ts = _ts(2026, 10, 21, 9, 0)
        _add(ts, "assistant", "hello")
        out = {}
        h = _H()
        h.send_json = lambda data, code=200: out.update(data)
        h._board_count = lambda sess: 0
        h.poll_get(dict(SESS), "0")
        self.assertEqual(out["messages"][0]["ts"], ts)

    def test_the_script_formats_in_the_zone_it_is_given_and_parses(self):
        js = server.CHAT_JS
        if js.startswith("<script>"):
            js = js[len("<script>"):-len("</script>")]
        for needle in ("function timeEl(ts)", "function dayBreak(ts)", "timeZone:TZ", "cfg.tz", "cfg.last_day", "m.ts||Date.now()/1000"):
            self.assertIn(needle, js)
        try:
            ok = subprocess.run(["node", "--version"], capture_output=True).returncode == 0
        except FileNotFoundError:
            ok = False
        if not ok:
            self.skipTest("node isn't available")
        path = os.path.join(os.environ["NORI_DATA_DIR"], "chat.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(js)
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_two_label_makers_agree(self):
        """The server's Today/Yesterday/weekday labels and the script's are the same shapes."""
        js = server.CHAT_JS
        self.assertIn("'Today'", js)
        self.assertIn("'Yesterday'", js)
        self.assertIn("weekday:'long',month:'short',day:'numeric'", js)


if __name__ == "__main__":
    unittest.main()

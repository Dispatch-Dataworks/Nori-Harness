# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""An instance whose assistant has its own name (Iron) must not say "Nori" where it means the assistant (2026-10-30, operator's own report: "message nori",
"turn on notifications for nori"). The product, the software and the repository are still Nori -- the About page, the Updates tab, "this harness" -- so this
checks the specific places that name the assistant, on the real pages, rather than banning the word. The phone app's half is core/AgentNameTests.kt."""
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_agent_name_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_agent_name_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import config  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Tester", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
config.set("workspace", WS, "setup_complete", True)
config.set("workspace", WS, "assistant_name", "Iron")
TOKEN = accounts.new_session(_admin, "127.0.0.1")

_httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
_httpd.daemon_threads = True
BASE = f"http://127.0.0.1:{_httpd.server_address[1]}"
threading.Thread(target=_httpd.serve_forever, daemon=True).start()


def get(path):
    req = urllib.request.Request(BASE + path, headers={"Cookie": f"ns={TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# Phrases that put "Nori" where the assistant is meant. Each was found in the pages and fixed; none may come back.
BAD = ("message nori", "when Nori messages", "Lets Nori", "Nori sees", "Nori can still", "Nori has no acting", "a file Nori didn't",
       "Nori writes", "added Nori to your home screen", "have Nori open", "alt='Nori:", "Nori's own database")


class OnAnInstanceCalledIron(unittest.TestCase):
    def test_the_composer_says_her_name(self):
        status, page = get("/")
        self.assertEqual(status, 200)
        self.assertIn("placeholder='message Iron'", page)
        self.assertNotIn("message nori", page.lower())

    def test_the_notification_prompt_uses_her_name_as_text_not_markup(self):
        page = get("/")[1]
        self.assertNotIn("when Nori messages", page)
        self.assertIn("'get a notification when '+((window.NORI_CHAT&&window.NORI_CHAT.assistant_name)||'your assistant')+' messages'", page)
        self.assertIn("bar.firstChild.textContent=", page, "set as text: a name can never inject markup into the bar")
        self.assertIn('"assistant_name": "Iron"', page)

    def test_a_name_with_markup_in_it_stays_inert_in_the_composer_and_the_config(self):
        config.set("workspace", WS, "assistant_name", "<b>x</b>'\"")
        try:
            page = get("/")[1]
            self.assertNotIn("<b>x</b>", page.replace("\\u003cb", ""))
            self.assertIn("placeholder='message &lt;b&gt;x&lt;/b&gt;&#x27;&quot;'", page)
        finally:
            config.set("workspace", WS, "assistant_name", "Iron")

    def test_the_avatar_preview_and_its_live_update_name_her(self):
        page = get("/")[1]
        self.assertIn("alt='Iron: ", page)
        self.assertIn("pi.alt=((window.NORI_CHAT&&window.NORI_CHAT.assistant_name)||'')+': '+newState", page)

    def test_no_page_puts_nori_where_the_assistant_is_meant(self):
        paths = ["/", "/files", "/history", "/photos", "/inventory", "/meals", "/trackers"]
        paths += [f"/settings?tab={t}" for t in ("pings", "chatvoice", "notify", "devices", "memory", "household", "subagents", "mcp", "models",
                                                 "sandbox", "memorybackend", "persona", "contexttuning", "updates", "peers", "tools", "webtools", "about")]
        seen = 0
        for path in paths:
            status, page = get(path)
            if status != 200:
                continue
            seen += 1
            for phrase in BAD:
                self.assertNotIn(phrase, page, f"{path} says {phrase!r}")
        self.assertGreater(seen, 10, "the pages actually rendered")

    def test_the_phone_is_told_the_assistants_name_so_the_app_can_use_it(self):
        import devices
        self.assertEqual(devices.phone_settings(UID)["assistant_name"], "Iron")
        config.set("workspace", WS, "assistant_name", "  " + "x" * 55 + "  ")
        try:
            self.assertEqual(devices.phone_settings(UID)["assistant_name"], "x" * 40)
        finally:
            config.set("workspace", WS, "assistant_name", "Iron")
        self.assertEqual(devices.phone_settings(99999)["assistant_name"], "", "an unknown user gets no name rather than an error")

    def test_the_product_is_still_called_nori_where_it_is_the_product(self):
        about = get("/settings?tab=updates")[1]
        self.assertIn("Once a day Nori asks GitHub", about)


if __name__ == "__main__":
    unittest.main()

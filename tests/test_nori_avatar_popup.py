# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The full-screen avatar pop-up on a change of expression is opt-in (avatar_popup_enabled, 2026-10-30, operator's own ask). Real server and config.
Pinned: off by default; the chat page tells its script which way it is set; the script's automatic path (not the tap on the header avatar) is the part
gated; the switch is on Settings -> Chat & voice and saves; it is per person."""
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_avatar_popup_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_avatar_popup_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import config  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Tester", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
config.set("workspace", WS, "setup_complete", True)
TOKEN = accounts.new_session(_admin, "127.0.0.1")
CSRF = accounts.get_session(TOKEN)["csrf"]
_inv, _raw = accounts.create_invite(WS, "Second", "member", UID)
MEMBER = accounts.accept_invite(_raw, "memberpass123")

_httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
_httpd.daemon_threads = True
BASE = f"http://127.0.0.1:{_httpd.server_address[1]}"
threading.Thread(target=_httpd.serve_forever, daemon=True).start()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http(path, form=None, token=TOKEN):
    data = None if form is None else urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST" if form is not None else "GET")
    req.add_header("Cookie", f"ns={token}")
    try:
        with _opener.open(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


class AvatarPopup(unittest.TestCase):
    def setUp(self):
        config.set("user", UID, "avatar_popup_enabled", False)

    def test_it_is_off_by_default(self):
        self.assertFalse(config._SPEC["avatar_popup_enabled"][0])
        self.assertEqual(config._SPEC["avatar_popup_enabled"][2], "user")

    def test_the_chat_page_says_which_way_it_is_set(self):
        self.assertIn("window.__noriPeekAuto__=false;", http("/")[1])
        config.set("user", UID, "avatar_popup_enabled", True)
        self.assertIn("window.__noriPeekAuto__=true;", http("/")[1])

    def test_only_the_automatic_path_is_gated_not_the_tap_on_the_avatar(self):
        page = http("/")[1]
        m = re.search(r"function nbShowPeek\(manual\)\{(.*?)\}function nbToggle", page, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        gate = body.index("if(!manual&&!window.__noriPeekAuto__)return;")
        self.assertLess(gate, body.index("p.classList.add('show')"), "checked before anything is shown")
        self.assertLess(gate, body.index("nbBackdrop(true)"))
        self.assertIn("nbShowPeek(true)", page, "the tap on the header avatar still opens it by hand")
        self.assertIn("nbShowPeek(false)", page, "the automatic callers are still there, just gated")

    def test_the_setting_is_on_chat_and_voice_unchecked_by_default_and_saves(self):
        status, page = http("/settings?tab=chatvoice")
        self.assertEqual(status, 200)
        self.assertIn("name=avatar_popup_enabled ", page)
        self.assertNotIn("name=avatar_popup_enabled checked", page)
        status, _ = http("/settings/chatvoice", {"csrf": CSRF, "tool_rounds_chat": "12", "tool_rounds_proactive": "4", "avatar_popup_enabled": "on"})
        self.assertIn(status, (200, 303))
        self.assertTrue(config.get("user", UID, "avatar_popup_enabled"))
        self.assertIn("name=avatar_popup_enabled checked", http("/settings?tab=chatvoice")[1])
        http("/settings/chatvoice", {"csrf": CSRF, "tool_rounds_chat": "12", "tool_rounds_proactive": "4"})
        self.assertFalse(config.get("user", UID, "avatar_popup_enabled"))

    def test_it_is_per_person(self):
        config.set("user", UID, "avatar_popup_enabled", True)
        self.assertFalse(config.get("user", MEMBER["id"], "avatar_popup_enabled"))

    def test_nori_can_read_it(self):
        import settings_tool
        sess = {"user_id": UID, "workspace_id": WS, "role": "admin"}
        got = settings_tool._get_settings_impl(sess)["settings"]["avatar_popup_enabled"]
        self.assertEqual((got["value"], got["scope"]), (False, "user"))


if __name__ == "__main__":
    unittest.main()

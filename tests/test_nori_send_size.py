# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""A long chat message (2026-11-02, found live on Sage): form() refused any urlencoded body over 64 KB by returning an EMPTY form, so the CSRF check that
follows found no token and answered "bad csrf token -- reload and try again". The person saw a security error for what was just a long message, and
retrying could never work. Now: a chat message may be up to MAX_MESSAGE_CHARS (its body limit is sized for that, worst case), a message beyond that is
refused with a plain statement of the length and the limit (and never stored), any other route's oversized body is a 413 that says it is too large, and a
genuinely wrong CSRF token is still a 403. Real server handlers and a scratch database; the model's turn is faked."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_sendsize_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_sendsize_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import conversation  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")
UID = _user["id"]
TOKEN = accounts.new_session(_user, "10.1.1.1")
CSRF = accounts.get_session(TOKEN)["csrf"]


class _P(server.Handler):
    """A Handler that has been sent one POST: just enough socket for do_POST to run."""

    def __init__(self, path, body: bytes, cookie=True):
        self.path = path
        self.headers = {"Content-Length": str(len(body)), "Content-Type": "application/x-www-form-urlencoded"}
        if cookie:
            self.headers["Cookie"] = "ns=" + TOKEN
        self.rfile, self.wfile = io.BytesIO(body), io.BytesIO()
        self.sent = []

    def ip(self):
        return "10.1.1.1"

    def send(self, code, body=b"", extra=None, *, ctype="text/html; charset=utf-8"):
        self.sent.append({"code": code, "body": body, "ctype": ctype})

    def reply(self):
        r = self.sent[-1]
        text = r["body"].decode("utf-8", "replace")
        return r["code"], (json.loads(text) if r["ctype"] == "application/json" else text)


def post(path, **fields):
    body = urllib.parse.urlencode(fields).encode()
    h = _P(path, body, cookie=fields.pop("_cookie", True))
    with patch.object(server.Handler, "_run_turn", lambda self, sess, user, voice=False: {"ok": True, "messages": []}):
        h.do_POST()
    return h.reply()


def stored():
    return [dict(r) for r in store.read(lambda c: c.execute("SELECT * FROM messages WHERE user_id=? AND role='user'", (UID,)).fetchall())]


class LongMessage(unittest.TestCase):
    def setUp(self):
        store.write(lambda c: c.execute("DELETE FROM messages"))

    def test_a_message_over_the_old_64kb_form_limit_is_sent_not_called_a_csrf_error(self):
        text = "word " * 16000                      # 80,000 characters, an urlencoded body well over 64 KB
        self.assertGreater(len(urllib.parse.urlencode({"csrf": CSRF, "text": text})), 65536)
        code, body = post("/send", csrf=CSRF, text=text)
        self.assertEqual(code, 200, body)
        self.assertEqual([len(m["content"]) for m in stored()], [len(text.strip())])

    def test_the_longest_allowed_message_in_the_worst_encoding_still_fits(self):
        text = "€" * server.MAX_MESSAGE_CHARS       # 3 bytes each, 9 characters each once percent-encoded
        code, body = post("/send", csrf=CSRF, text=text)
        self.assertEqual(code, 200, body)
        self.assertEqual(len(stored()), 1)

    def test_a_message_beyond_the_limit_is_refused_plainly_and_not_stored(self):
        text = "a" * (server.MAX_MESSAGE_CHARS + 1)
        code, body = post("/send", csrf=CSRF, text=text)
        self.assertEqual(code, 413)
        self.assertFalse(body["ok"])
        self.assertIn("100,000", body["error"])
        self.assertIn(f"{len(text):,}", body["error"])
        self.assertNotIn("csrf", body["error"].lower())
        self.assertEqual(stored(), [])

    def test_a_body_larger_than_even_that_is_refused_plainly_too(self):
        code, body = post("/send", csrf=CSRF, text="€" * (server.MAX_MESSAGE_CHARS * 2))
        self.assertEqual(code, 413)
        self.assertNotIn("csrf", body["error"].lower())
        self.assertEqual(stored(), [])

    def test_a_wrong_csrf_token_is_still_a_403(self):
        code, body = post("/send", csrf="not-the-token", text="hello")
        self.assertEqual(code, 403)
        self.assertIn("csrf", body["error"].lower())
        self.assertEqual(stored(), [])

    def test_a_missing_token_on_a_big_message_is_a_403_not_a_413(self):
        code, body = post("/send", text="x" * 20000)
        self.assertEqual(code, 403)

    def test_signed_out_is_still_signed_out_however_big(self):
        h = _P("/send", urllib.parse.urlencode({"csrf": CSRF, "text": "x" * 100000}).encode(), cookie=False)
        h.do_POST()
        code, body = h.reply()
        self.assertEqual(code, 401)
        self.assertEqual(body["error"], "not signed in")


class EveryOtherRoute(unittest.TestCase):
    def test_other_forms_keep_their_64kb_limit_but_say_what_is_wrong(self):
        code, body = post("/board/notes", csrf=CSRF, title="t", body="x" * 70000)
        self.assertEqual(code, 413)
        self.assertIn("too large", body.lower())
        self.assertNotIn("csrf", body.lower())

    def test_a_json_route_answers_in_json(self):
        code, body = post("/retry", csrf=CSRF, user_id="1", junk="x" * 70000)
        self.assertEqual(code, 413)
        self.assertFalse(body["ok"])
        self.assertIn("too large", body["error"])

    def test_a_normal_form_is_untouched(self):
        code, body = post("/retry", csrf=CSRF, user_id="999999")
        self.assertEqual(code, 404)


class TheComposer(unittest.TestCase):
    def test_the_page_knows_the_limit_and_keeps_the_draft_when_it_is_exceeded(self):
        js = server.CHAT_JS
        self.assertIn(f"var MAXC={server.MAX_MESSAGE_CHARS};", js)
        guard = js.index("text.length>MAXC")
        self.assertLess(guard, js.index("inp.value=''", guard), "the check comes before the box is cleared, so the draft survives")


if __name__ == "__main__":
    unittest.main()

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Staying signed in (2026-11-07). A session used to end 24 hours after sign-in however busy it was, so people signed in far too often. Now a
session is an idle window that using it moves forward (accounts.touch_session), "keep me signed in" at login makes the window 30 days, and no
session lives past 90 days. Pinned here, against a real server: the checkbox, the cookie's lifetime at login, that a page or a form extends the
session and re-sends the cookie with the new lifetime, that the chat's timer polls do not (an open tab must not keep a session alive), that the end
moves at most a few times a day, the 90-day ceiling, expiry, and that logout still clears the cookie. Real server and SQLite; no network."""
import http.client
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_sessions_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_sessions_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import config  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Tester", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
config.set("workspace", WS, "setup_complete", True)

_httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
_httpd.daemon_threads = True
PORT = _httpd.server_address[1]
threading.Thread(target=_httpd.serve_forever, daemon=True).start()

DAY = 86400


def request(path, *, method="GET", form=None, token=None):
    """(status, [Set-Cookie header values], body) with no redirects followed."""
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30)
    headers = {"Cookie": f"ns={token}"} if token else {}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=data, headers=headers)
    r = conn.getresponse()
    body = r.read().decode("utf-8", "replace")
    cookies = [v for k, v in r.getheaders() if k.lower() == "set-cookie"]
    conn.close()
    return r.status, cookies, body


def login(keep=None):
    form = {"display_name": "Tester", "password": "testpass123"}
    if keep is not None:
        form["keep"] = "1"
    status, cookies, _ = request("/login", method="POST", form=form)
    assert status == 303 and cookies, (status, cookies)
    token = cookies[0].split(";")[0].split("=", 1)[1]
    return token, cookies[0]


def max_age(cookie: str) -> int:
    return int(cookie.split("Max-Age=")[1].split(";")[0])


def row(token):
    return dict(store.read(lambda c: c.execute("SELECT * FROM sessions WHERE token_hash=?", (accounts._token_hash(token),)).fetchone()))


def set_row(token, **cols):
    sets = ", ".join(f"{k}=?" for k in cols)
    store.write(lambda c: c.execute(f"UPDATE sessions SET {sets} WHERE token_hash=?", (*cols.values(), accounts._token_hash(token))))


class Login(unittest.TestCase):
    def test_the_login_page_has_the_checkbox(self):
        status, _, body = request("/login")
        self.assertEqual(status, 200)
        self.assertIn("name=keep", body)
        self.assertIn("keep me signed in", body)
        self.assertNotIn("name=keep value=1 checked", body, "off unless the person ticks it")

    def test_an_ordinary_sign_in_lasts_a_day_of_idleness(self):
        token, cookie = login()
        self.assertEqual(max_age(cookie), accounts.SESSION_TTL_S)
        r = row(token)
        self.assertEqual(r["keep"], 0)
        self.assertAlmostEqual(r["expires_ts"] - r["created_ts"], DAY, delta=5)

    def test_keep_me_signed_in_lasts_thirty_days_of_idleness(self):
        token, cookie = login(keep=True)
        self.assertEqual(max_age(cookie), 30 * DAY)
        r = row(token)
        self.assertEqual(r["keep"], 1)
        self.assertAlmostEqual(r["expires_ts"] - r["created_ts"], 30 * DAY, delta=5)

    def test_a_wrong_password_signs_nobody_in(self):
        status, cookies, body = request("/login", method="POST", form={"display_name": "Tester", "password": "nope", "keep": "1"})
        self.assertEqual((status, cookies), (200, []))
        self.assertIn("wrong name or password", body)


class Extending(unittest.TestCase):
    def test_using_a_session_moves_its_end_and_resends_the_cookie(self):
        token, _ = login()
        set_row(token, expires_ts=time.time() + DAY / 2)     # half the window used up
        status, cookies, _ = request("/history", token=token)
        self.assertIn(status, (200, 303))
        self.assertEqual(len(cookies), 1)
        self.assertIn(f"ns={token}", cookies[0])
        self.assertIn("HttpOnly", cookies[0])
        self.assertAlmostEqual(max_age(cookies[0]), DAY, delta=10)
        self.assertAlmostEqual(row(token)["expires_ts"], time.time() + DAY, delta=10)

    def test_a_kept_session_is_extended_by_thirty_days(self):
        token, _ = login(keep=True)
        set_row(token, expires_ts=time.time() + 10 * DAY)
        _, cookies, _ = request("/history", token=token)
        self.assertAlmostEqual(max_age(cookies[0]), 30 * DAY, delta=10)
        self.assertAlmostEqual(row(token)["expires_ts"], time.time() + 30 * DAY, delta=10)

    def test_a_form_extends_it_too(self):
        token, _ = login()
        set_row(token, expires_ts=time.time() + DAY / 2)
        csrf = row(token)["csrf"]
        status, cookies, _ = request("/approval", method="POST", form={"csrf": csrf, "source": "none", "id": "0", "decision": "approve"}, token=token)
        self.assertEqual(len(cookies), 1, status)
        self.assertAlmostEqual(row(token)["expires_ts"], time.time() + DAY, delta=10)

    def test_it_only_moves_after_a_tenth_of_the_window_so_a_busy_session_is_not_rewritten_every_request(self):
        token, _ = login()
        before = row(token)["expires_ts"]
        _, cookies, _ = request("/history", token=token)
        self.assertEqual(cookies, [])
        self.assertEqual(row(token)["expires_ts"], before)
        set_row(token, expires_ts=time.time() + DAY * 0.95)     # 5% used: still too soon
        _, cookies, _ = request("/history", token=token)
        self.assertEqual(cookies, [])

    def test_the_chats_timer_polls_do_not_keep_a_session_alive(self):
        token, _ = login()
        soon = time.time() + DAY / 2
        set_row(token, expires_ts=soon)
        for path in ("/poll?since=0", "/poll?since=0&only=approvals", "/board/fragment"):
            _, cookies, _ = request(path, token=token)
            self.assertEqual(cookies, [], path)
        self.assertEqual(row(token)["expires_ts"], soon)

    def test_the_end_never_passes_ninety_days_after_sign_in(self):
        token, _ = login(keep=True)
        now = time.time()
        set_row(token, created_ts=now - accounts.SESSION_MAX_S + 5 * DAY, expires_ts=now + DAY)
        _, cookies, _ = request("/history", token=token)
        self.assertEqual(len(cookies), 1)
        self.assertAlmostEqual(row(token)["expires_ts"], now + 5 * DAY, delta=10, msg="capped, not the full 30 days")
        self.assertAlmostEqual(max_age(cookies[0]), 5 * DAY, delta=10)

    def test_a_session_ninety_days_old_is_over_however_recently_it_was_used(self):
        token, _ = login(keep=True)
        set_row(token, created_ts=time.time() - accounts.SESSION_MAX_S - 1)
        self.assertIsNone(accounts.get_session(token))
        status, cookies, _ = request("/history", token=token)
        self.assertEqual((status, cookies), (303, []))

    def test_an_expired_session_is_not_revived_by_a_request(self):
        token, _ = login()
        set_row(token, expires_ts=time.time() - 5)
        status, cookies, _ = request("/history", token=token)
        self.assertEqual((status, cookies), (303, []))
        self.assertLess(row(token)["expires_ts"], time.time())
        self.assertIsNone(accounts.get_session(token))

    def test_a_session_idle_for_longer_than_its_window_ends(self):
        plain, _ = login()
        kept, _ = login(keep=True)
        for tok in (plain, kept):
            set_row(tok, created_ts=time.time() - 2 * DAY, expires_ts=time.time() + DAY)
        # A day and a half later: the ordinary window (a day since last used) has gone, the kept one has not.
        for tok, window in ((plain, DAY), (kept, 30 * DAY)):
            set_row(tok, expires_ts=time.time() + window - 1.5 * DAY)
        self.assertIsNone(accounts.get_session(plain))
        self.assertIsNotNone(accounts.get_session(kept))


class Leaving(unittest.TestCase):
    def test_signing_out_clears_the_cookie_and_the_session(self):
        token, _ = login(keep=True)
        set_row(token, expires_ts=time.time() + DAY)             # due an extension, which sign-out must not send
        status, cookies, _ = request("/logout", method="POST", form={"csrf": row(token)["csrf"]}, token=token)
        self.assertEqual(status, 303)
        self.assertEqual(len(cookies), 1)
        self.assertIn("Max-Age=0", cookies[0])
        self.assertIsNone(accounts.get_session(token))

    def test_deactivating_a_person_ends_their_kept_sessions(self):
        _inv, raw = accounts.create_invite(WS, "Claire", "member", UID)
        claire = accounts.accept_invite(raw, "memberpass123")
        token = accounts.new_session(claire, "127.0.0.1", keep=True)
        self.assertIsNotNone(accounts.get_session(token))
        accounts.deactivate_user(claire["id"])
        self.assertIsNone(accounts.get_session(token))

    def test_the_other_ways_in_still_work_without_the_checkbox(self):
        token = accounts.new_session(_admin, "127.0.0.1")
        self.assertEqual(accounts.get_session(token)["keep"], 0)
        self.assertEqual(accounts.session_ttl(False), accounts.SESSION_TTL_S)
        self.assertEqual(accounts.session_ttl(True), accounts.KEEP_TTL_S)


if __name__ == "__main__":
    unittest.main()

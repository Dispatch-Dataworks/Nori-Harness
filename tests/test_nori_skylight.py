# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Skylight, the family's wall calendar (2026-11-02, skylight.py + the vendored skylight_calendar library).

Real SQLite, the real tool registry, the real server handlers and the real library over a real socket: the only fake is Skylight itself, a small
server on 127.0.0.1 that answers the way app.ourskylight.com does (sign-in pages, OAuth token endpoint, the JSON:API the frame's apps use)."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_skylight_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_skylight_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import config  # noqa: E402
import peers  # noqa: E402
import server  # noqa: E402
import skylight  # noqa: E402
import skylight_calendar as sky  # noqa: E402
import store  # noqa: E402
import tools  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Tester", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
_inv_id, _inv_tok = accounts.create_invite(WS, "Second", "member", UID)
UID2 = accounts.accept_invite(_inv_tok, "memberpass123")["id"]
SESS = {"user_id": UID, "workspace_id": WS, "role": "admin", "csrf": "csrf-tok", "display_name": "Tester"}
MEM = {"user_id": UID2, "workspace_id": WS, "role": "member", "csrf": "csrf-mem", "display_name": "Second"}

PASSWORD = "correct-horse-battery"
FRAME = {"id": "5455113", "type": "frame", "attributes": {"name": "smith-7772", "household_name": "The Smiths", "timezone": "America/Chicago"}}
PROFILES = [
    {"id": "101", "type": "category", "attributes": {"label": "Kayla", "color": "#915EA1", "linked_to_profile": True, "selected_for_chore_chart": True}},
    {"id": "102", "type": "category", "attributes": {"label": "Garrett", "color": "#CB434C", "linked_to_profile": True, "selected_for_chore_chart": True}},
]
EVENT = {"id": "ev-1", "type": "calendar_event", "attributes": {
    "summary": "Dentist", "starts_at": "2026-12-05T09:30:00.000-06:00", "ends_at": "2026-12-05T10:30:00.000-06:00", "all_day": False,
    "location": "Downtown", "recurring": False, "timezone": "America/Chicago", "source": "skylight", "editable": True},
    "relationships": {"categories": {"data": [{"id": "101", "type": "category"}]}}}


class FakeSkylight:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []           # (method, path, query, body, headers)
        self.state = ""
        self.access, self.refresh, self.issued = set(), "", 0
        self.refresh_calls = 0
        self.refresh_delay = 0.0
        self.refuse_refresh = False
        self.items = [{"id": "1", "label": "Eggs", "status": "pending"}, {"id": "2", "label": "Milk", "status": "completed"}]

    def issue(self):
        self.issued += 1
        token, self.refresh = f"access-{self.issued}", f"refresh-{self.issued}"
        self.access.add(token)
        return {"access_token": token, "refresh_token": self.refresh, "token_type": "Bearer", "expires_in": 7200, "created_at": int(time.time())}

    def answer(self, method, path, query, body, headers):
        cookie = headers.get("cookie", "") or ""
        if path == "/oauth/authorize":
            if query.get("state"):
                self.state = query["state"][0]
            if "skylightcloud_session=authed" in cookie:
                return 302, {"Location": f"https://ourskylight.com/welcome?code=code-xyz&state={self.state}"}, b""
            return 302, {"Location": "/auth/session/new", "Set-Cookie": "skylightcloud_session=anon; Path=/"}, b""
        if path == "/auth/session/new":
            return 200, {"Content-Type": "text/html"}, b'<form><input type="hidden" name="authenticity_token" value="csrf-123"></form>'
        if path == "/auth/session":
            form = urllib.parse.parse_qs(body.decode())
            if form.get("authenticity_token") != ["csrf-123"] or form.get("password") != [PASSWORD]:
                return 200, {"Content-Type": "text/html"}, b"Invalid email or password"
            return 302, {"Location": "/oauth/authorize?client_id=skylight-mobile", "Set-Cookie": "skylightcloud_session=authed; Path=/"}, b""
        if path == "/oauth/token":
            form = urllib.parse.parse_qs(body.decode())
            grant = form.get("grant_type", [""])[0]
            if grant == "authorization_code" and form.get("code") == ["code-xyz"] and form.get("code_verifier"):
                return self.js(200, self.issue())
            if grant == "refresh_token":
                self.refresh_calls += 1
                time.sleep(self.refresh_delay)
                if self.refuse_refresh or form.get("refresh_token") != [self.refresh]:
                    return self.js(401, {"error": "invalid_grant"})
                return self.js(200, self.issue())
            return self.js(400, {"error": "invalid_grant"})
        auth = headers.get("authorization", "") or ""
        if not auth.startswith("Bearer ") or auth[7:] not in self.access:
            return self.js(401, {"errors": ["Invalid token"]})
        return self.api(method, path, query, json.loads(body) if body else None)

    def api(self, m, path, q, body):
        f = f"/api/frames/{FRAME['id']}"
        if path == "/api/user":
            return self.js(200, {"data": {"id": "1", "type": "user", "attributes": {"email": "me@example.com"}}})
        if path == "/api/frames":
            return self.js(200, {"data": [FRAME]})
        if path == f:
            return self.js(200, {"data": FRAME})
        if path == f + "/categories":
            return self.js(200, {"data": PROFILES})
        if path == f + "/calendar_events" and m == "GET":
            return self.js(200, {"data": [EVENT]})
        if path == f + "/calendar_events" and m == "POST":
            return self.js(200, {"data": {**EVENT, "id": "ev-new", "attributes": {**EVENT["attributes"], "summary": body["summary"],
                                 "starts_at": body["starts_at"], "ends_at": body["ends_at"], "all_day": body.get("all_day", False)},
                                 "relationships": {"categories": {"data": [{"id": i, "type": "category"} for i in body.get("category_ids") or []]}}}})
        if path.startswith(f + "/calendar_events/") and m == "PUT":
            return self.js(200, {"data": {**EVENT, "attributes": {**EVENT["attributes"], **{k: v for k, v in body.items() if k == "summary"}}}})
        if path.startswith(f + "/calendar_events/") and m == "DELETE":
            return 204, {}, b""
        if path == f + "/lists" and m == "GET":
            return self.js(200, {"data": [{"id": "L1", "type": "list", "attributes": {"label": "Grocery List", "kind": "shopping", "default_grocery_list": True},
                                           "relationships": {"list_items": {"data": [{"id": i["id"], "type": "list_item"} for i in self.items]}}},
                                          {"id": "L2", "type": "list", "attributes": {"label": "To-Do List", "kind": "to_do"},
                                           "relationships": {"list_items": {"data": []}}}],
                                 "included": [{"id": i["id"], "type": "list_item", "attributes": {"label": i["label"], "status": i["status"]},
                                               "relationships": {"list": {"data": {"id": "L1", "type": "list"}}}} for i in self.items]})
        if path == f + "/lists/L1/list_items" and m == "POST":
            item = {"id": str(len(self.items) + 1), "label": body["label"], "status": "pending"}
            self.items.append(item)
            return self.js(201, {"data": {"id": item["id"], "type": "list_item", "attributes": {"label": item["label"], "status": "pending"}}})
        if path.startswith(f + "/lists/L1/list_items/") and m == "PUT":
            iid = path.rsplit("/", 1)[1]
            item = next(i for i in self.items if i["id"] == iid)
            item.update({k: v for k, v in body.items() if k in ("status", "label")})
            return self.js(200, {"data": {"id": iid, "type": "list_item", "attributes": {"label": item["label"], "status": item["status"]}}})
        if path == f + "/chores" and m == "GET":
            return self.js(200, {"data": [
                {"id": "c1", "type": "chore", "attributes": {"summary": "Feed the cat", "status": "pending", "start": "2026-12-05", "reward_points": 1},
                 "relationships": {"category": {"data": {"id": "101", "type": "category"}}}},
                {"id": "777-2026-12-05-0600", "type": "chore", "attributes": {"summary": "Make bed", "status": "complete", "start": "2026-12-05",
                 "start_time": "06:00", "recurring": True}, "relationships": {"category": {"data": {"id": "102", "type": "category"}}}}]})
        if path == f + "/chores/create_multiple":
            ids = body.get("category_ids") or [None]
            return self.js(201, {"data": [{"id": f"n{n}", "type": "chore", "attributes": {"summary": body["summary"], "status": "pending",
                                           "start": body.get("start"), "up_for_grabs": body.get("up_for_grabs", False)},
                                           "relationships": {"category": {"data": {"id": i, "type": "category"} if i else None}}} for n, i in enumerate(ids)]})
        if path.endswith("/completions"):
            return self.js(200, {"data": {"id": path.split("/")[-2], "type": "chore", "attributes": {"summary": "Make bed", "status": body["status"]},
                                          "relationships": {"category": {"data": {"id": "102", "type": "category"}}}},
                                 "meta": {"reward_points": {"current_point_balance": 6}}})
        if path == f + "/meals/categories":
            return self.js(200, {"data": [{"id": "M1", "type": "meal_category", "attributes": {"label": "Breakfast", "enabled": True}},
                                          {"id": "M3", "type": "meal_category", "attributes": {"label": "Dinner", "enabled": True}}]})
        if path == f + "/meals/sittings" and m == "GET":
            return self.js(200, {"data": [], "included": []})
        if path == f + "/meals/sittings" and m == "POST":
            return self.js(201, {"data": [{"id": "S1", "type": "meal_sitting", "attributes": {"summary": body.get("summary"), "instances": [body["date"]]},
                                           "relationships": {"meal_category": {"data": {"id": body["meal_category_id"], "type": "meal_category"}}}}]})
        if path == f + "/rewards":
            return self.js(200, {"data": [{"id": "r1", "type": "reward", "attributes": {"name": "Ice cream", "point_value": 3},
                                           "relationships": {"category": {"data": {"id": "101", "type": "category"}}}}]})
        if path == f + "/reward_points":
            return self.js(200, [{"category_id": 101, "current_point_balance": 4}, {"category_id": 102, "current_point_balance": 0}])
        return self.js(500, {"error": f"unhandled {m} {path}"})

    @staticmethod
    def js(code, obj):
        return code, {"Content-Type": "application/json"}, json.dumps(obj).encode()

    def posts(self, suffix):
        return [r for r in self.requests if r[0] in ("POST", "PUT") and r[1].endswith(suffix)]


FAKE = FakeSkylight()


class _Bridge(BaseHTTPRequestHandler):
    def _serve(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        parts = urllib.parse.urlsplit(self.path)
        headers = {k.lower(): v for k, v in self.headers.items()}
        with FAKE.lock:
            FAKE.requests.append((self.command, parts.path, urllib.parse.parse_qs(parts.query), body, headers))
        code, hdrs, out = FAKE.answer(self.command, parts.path, urllib.parse.parse_qs(parts.query), body, headers)
        self.send_response(code)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

    def log_message(self, *args):
        pass


_SRV = ThreadingHTTPServer(("127.0.0.1", 0), _Bridge)
threading.Thread(target=_SRV.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{_SRV.server_address[1]}"
_REAL_NEW_HTTP = skylight._new_http
skylight.BASE_URL = BASE
skylight._new_http = lambda: sky.StdlibClient(timeout=10, allowed_hosts={"127.0.0.1"}, allow_http=True)


def _reset():
    for t in ("skylight_connection", "skylight_log"):
        store.write(lambda c, t=t: c.execute(f"DELETE FROM {t}"))
    config.set("workspace", WS, "skylight_enabled", False)
    skylight._profiles.clear()
    skylight._browser_flows.clear()
    FAKE.__init__()


def _connect():
    ok, msg = skylight.connect_with_password(WS, UID, "me@example.com", PASSWORD)
    assert ok, msg


def _tool(name, args=None, sess=MEM):
    return tools.dispatch(name, args or {}, sess)


def _raw_db() -> bytes:
    data_dir = os.environ["NORI_DATA_DIR"]
    out = b""
    for dp, _, fns in os.walk(data_dir):
        for fn in fns:
            with open(os.path.join(dp, fn), "rb") as fh:
                out += fh.read()
    return out


class _H(server.Handler):
    def __init__(self):
        self.headers, self.rfile, self.wfile = {}, io.BytesIO(), io.BytesIO()
        self.sent, self.page = [], None

    def send(self, code, body=b"", extra=None, *, ctype="text/html; charset=utf-8"):
        self.sent.append(code)

    def _settings_response(self, sess, tab, main):
        self.page = main


class OffUntilConnected(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_no_tools_and_no_calls_before_it_is_connected(self):
        names = {s["function"]["name"] for s in tools.active_schemas(MEM)}
        self.assertFalse(any(n.startswith("skylight_") for n in names))
        self.assertFalse(skylight.enabled(WS))
        self.assertEqual(FAKE.requests, [])

    def test_switching_it_on_needs_a_connection(self):
        self.assertFalse(skylight.set_enabled(WS, True, UID)[0])

    def test_no_skylight_tool_is_peer_requestable(self):
        self.assertFalse([n for n in peers._PEER_REQUESTABLE if n.startswith("skylight")])


class SigningIn(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_the_password_signs_in_once_and_is_never_kept(self):
        ok, msg = skylight.connect_with_password(WS, UID, "me@example.com", PASSWORD)
        self.assertTrue(ok, msg)
        self.assertIn("The Smiths", msg)
        st = skylight.status(WS)
        self.assertTrue(st["connected"] and st["enabled"])
        self.assertEqual((st["frame_name"], st["timezone"]), ("The Smiths", "America/Chicago"))
        self.assertNotIn("access", json.dumps(st))
        raw = _raw_db()
        self.assertNotIn(PASSWORD.encode(), raw, "the password is nowhere on disk")
        self.assertNotIn(b"refresh-1", raw, "the session is stored encrypted")
        self.assertEqual(skylight._load(WS)["credentials"]["refresh_token"], "refresh-1")
        self.assertNotIn(PASSWORD, json.dumps(skylight.recent_log(WS)))
        names = {s["function"]["name"] for s in tools.active_schemas(MEM)}
        self.assertIn("skylight_calendar", names, "every member's assistant gets it")

    def test_a_wrong_password_keeps_nothing(self):
        ok, msg = skylight.connect_with_password(WS, UID, "me@example.com", "nope")
        self.assertFalse(ok)
        self.assertIn("rejected", msg)
        self.assertEqual(skylight._load(WS), {})
        self.assertFalse(skylight.enabled(WS))

    def test_signing_in_on_skylights_own_page(self):
        url = skylight.browser_start(WS, UID)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertTrue(url.startswith(BASE + "/oauth/authorize"))
        self.assertEqual(q["code_challenge_method"], ["S256"])
        state = q["state"][0]
        ok, msg = skylight.browser_finish(WS, UID, f"https://ourskylight.com/welcome?code=code-xyz&state=wrong")
        self.assertFalse(ok, "a pasted address from another sign-in is refused")
        ok, msg = skylight.browser_finish(WS, UID, f"https://ourskylight.com/welcome?code=code-xyz&state={state}")
        self.assertTrue(ok, msg)
        self.assertTrue(skylight.enabled(WS))
        self.assertFalse(skylight.status(WS)["browser_pending"])

    def test_a_browser_sign_in_expires(self):
        skylight.browser_start(WS, UID)
        started, uid, flow = skylight._browser_flows[WS]
        skylight._browser_flows[WS] = (started - skylight.BROWSER_FLOW_TTL_S - 1, uid, flow)
        ok, msg = skylight.browser_finish(WS, UID, "code-xyz")
        self.assertFalse(ok)
        self.assertIn("expired", msg)

    def test_the_real_transport_only_talks_to_skylight(self):
        http = _REAL_NEW_HTTP()
        with self.assertRaises(sky.TransportError):
            http.get(BASE + "/api/user")
        with self.assertRaises(sky.TransportError):
            http.get("https://example.com/api/user")
        self.assertEqual(http.allowed_hosts, {"app.ourskylight.com"})

    def test_disconnect_forgets_the_session(self):
        _connect()
        skylight.disconnect(WS, UID)
        self.assertEqual(skylight._load(WS), {})
        self.assertFalse(skylight.enabled(WS))
        self.assertIn("no such tool", _tool("skylight_calendar")["error"], "a disconnected Skylight's tools are not offered at all")
        self.assertIn("isn't connected", skylight._calendar_impl(MEM)["error"], "and the chokepoint refuses on its own too")


class Calendar(unittest.TestCase):
    def setUp(self):
        _reset()
        _connect()

    def test_reading_the_calendar(self):
        out = _tool("skylight_calendar", {"days": 3})
        self.assertEqual(out["events"][0]["title"], "Dentist")
        self.assertEqual(out["events"][0]["who"], ["Kayla"])
        self.assertIn("not instructions", out["note"])
        self.assertEqual(out["timezone"], "America/Chicago")
        get = [r for r in FAKE.requests if r[1].endswith("/calendar_events") and r[0] == "GET"][-1]
        self.assertEqual(get[2]["timezone"], ["America/Chicago"])

    def test_a_time_without_an_offset_is_the_households_wall_clock(self):
        out = _tool("skylight_add_event", {"title": "Piano", "start": "2026-12-05T15:30", "who": ["kayla"]})
        self.assertTrue(out.get("ok"), out)
        body = json.loads(FAKE.posts("/calendar_events")[-1][3])
        self.assertTrue(body["starts_at"].startswith("2026-12-05T15:30") and body["starts_at"].endswith("-06:00"), body["starts_at"])
        self.assertTrue(body["ends_at"].startswith("2026-12-05T16:30"), "an hour long unless told otherwise")
        self.assertEqual(body["category_ids"], ["101"])

    def test_an_all_day_event_through_a_last_day(self):
        _tool("skylight_add_event", {"title": "Grandma visiting", "start": "2026-12-05", "end": "2026-12-07"})
        body = json.loads(FAKE.posts("/calendar_events")[-1][3])
        self.assertTrue(body["all_day"])
        self.assertTrue(body["ends_at"].startswith("2026-12-08"), "Skylight's end is the morning after the last day")

    def test_an_unknown_person_is_refused_before_anything_is_sent(self):
        before = len(FAKE.posts("/calendar_events"))
        out = _tool("skylight_add_event", {"title": "Piano", "start": "2026-12-05T15:30", "who": ["Zed"]})
        self.assertIn("Kayla, Garrett", out["error"])
        self.assertEqual(len(FAKE.posts("/calendar_events")), before)

    def test_a_bad_date_is_refused(self):
        self.assertIn("2026-11-05T15:30", _tool("skylight_add_event", {"title": "x", "start": "next tuesday"})["error"])

    def test_changing_and_removing_an_event(self):
        self.assertTrue(_tool("skylight_update_event", {"event_id": "ev-1", "title": "Dentist (moved)"})["ok"])
        self.assertTrue(_tool("skylight_delete_event", {"event_id": "ev-1"})["ok"])
        self.assertTrue(any(r[0] == "DELETE" and r[1].endswith("/calendar_events/ev-1") for r in FAKE.requests))
        self.assertTrue(tools.is_consequential("skylight_delete_event"))


class ListsChoresMeals(unittest.TestCase):
    def setUp(self):
        _reset()
        _connect()

    def test_the_grocery_list_by_default_and_no_duplicates(self):
        out = _tool("skylight_add_to_list", {"items": ["Bread", "eggs"]})
        self.assertEqual(out["added"], ["Bread"])
        self.assertEqual(out["already_on_it"], ["eggs"])
        lists = _tool("skylight_lists", {"list": "grocery"})["lists"]
        self.assertEqual([i["item"] for i in lists[0]["items"]], ["Eggs", "Bread"], "a checked-off item is not shown as still needed")

    def test_checking_off_by_name(self):
        out = _tool("skylight_check_off", {"items": ["Eggs", "Caviar"]})
        self.assertEqual((out["checked_off"], out["not_found"]), (["Eggs"], ["Caviar"]))
        self.assertEqual(json.loads(FAKE.posts("/list_items/1")[-1][3])["status"], "completed")

    def test_an_unknown_list(self):
        self.assertIn("Grocery List, To-Do List", _tool("skylight_lists", {"list": "Hardware"})["error"])

    def test_chores_for_one_person_and_completing_one(self):
        out = _tool("skylight_chores", {"who": "Garrett"})
        self.assertEqual([c["chore"] for c in out["chores"]], ["Make bed"])
        done = _tool("skylight_complete_chore", {"chore_id": "777-2026-12-05-0600"})
        self.assertEqual(done["point_balance"], 6)
        put = FAKE.posts("/chores/777/completions")[-1]
        self.assertEqual(json.loads(put[3])["instance_date"], "2026-12-05", "an occurrence id is split into its series and day")

    def test_a_chore_for_no_one_is_up_for_grabs(self):
        out = _tool("skylight_add_chore", {"chore": "Rake leaves", "date": "2026-12-06"})
        self.assertTrue(out["ok"], out)
        body = json.loads(FAKE.posts("/chores/create_multiple")[-1][3])
        self.assertTrue(body["up_for_grabs"])

    def test_planning_a_meal(self):
        out = _tool("skylight_set_meal", {"date": "2026-12-05", "what": "Tacos", "meal": "dinner"})
        self.assertEqual((out["meal"], out["what"]), ("Dinner", "Tacos"))
        self.assertIn("Breakfast, Dinner", _tool("skylight_set_meal", {"date": "2026-12-05", "what": "x", "meal": "Brunch"})["error"])

    def test_reward_points_by_name(self):
        out = _tool("skylight_rewards")
        self.assertEqual(out["points"], {"Kayla": 4, "Garrett": 0})


class TheSession(unittest.TestCase):
    def setUp(self):
        _reset()
        _connect()

    def _expire(self):
        d = skylight._load(WS)
        d["credentials"]["expires_at"] = time.time() - 10
        skylight._save(WS, d)

    def test_a_refreshed_session_is_saved_back(self):
        self._expire()
        self.assertIn("events", _tool("skylight_calendar"))
        self.assertEqual(skylight._load(WS)["credentials"]["refresh_token"], "refresh-2")

    def test_only_one_refresh_when_two_assistants_ask_at_once(self):
        # Skylight rotates the refresh token on use: two refreshes racing would sign one of them out. The lock makes the second wait and reuse.
        self._expire()
        FAKE.refresh_delay = 0.3
        results = []
        threads = [threading.Thread(target=lambda: results.append(_tool("skylight_lists"))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(FAKE.refresh_calls, 1)
        self.assertTrue(all("lists" in r for r in results), results)

    def test_when_skylight_signs_nori_out(self):
        self._expire()
        FAKE.refuse_refresh = True
        out = _tool("skylight_calendar")
        self.assertIn("reconnect", out["error"])
        self.assertFalse(skylight.enabled(WS))
        self.assertTrue(skylight.status(WS)["signed_out"])
        self.assertNotIn("credentials", skylight._load(WS))
        h = _H()
        h.skylight_admin_form(SESS)
        self.assertIn("signed", h.page)
        self.assertTrue(skylight.connect_with_password(WS, UID, "me@example.com", PASSWORD)[0], "signing in again fixes it")


class SettingsPage(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_admins_only(self):
        h = _H()
        h.skylight_admin_form(MEM)
        self.assertEqual(h.sent, [403])
        h = _H()
        h.skylight_post(MEM, "/admin/skylight/connect", {"email": "me@example.com", "password": PASSWORD})
        self.assertEqual(h.sent, [403])
        self.assertFalse(skylight.enabled(WS))

    def test_connect_switch_and_disconnect_through_the_page(self):
        h = _H()
        h.skylight_admin_form(SESS)
        self.assertIn("not connected", h.page)
        h = _H()
        h.skylight_post(SESS, "/admin/skylight/connect", {"email": "me@example.com", "password": PASSWORD})
        self.assertIn("connected to The Smiths", h.page)
        self.assertNotIn(PASSWORD, h.page)
        self.assertNotIn("access-1", h.page)
        h = _H()
        h.skylight_post(SESS, "/admin/skylight/enabled", {})
        self.assertFalse(skylight.enabled(WS))
        self.assertTrue(skylight.connected(WS), "off keeps the connection")
        h = _H()
        h.skylight_post(SESS, "/admin/skylight/enabled", {"on": "1"})
        self.assertTrue(skylight.enabled(WS))
        h = _H()
        h.skylight_post(SESS, "/admin/skylight/disconnect", {})
        self.assertFalse(skylight.connected(WS))
        self.assertIn("connect", [r["action"] for r in skylight.recent_log(WS)])

    def test_its_in_the_admin_menu(self):
        groups = server.Handler._settings_groups(SESS)
        self.assertIn(("skylight", "Skylight"), [t for _, tabs in groups for t in tabs])


if __name__ == "__main__":
    unittest.main()

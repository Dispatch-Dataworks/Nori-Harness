# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The device routes over a REAL HTTP server on a real port (2026-10-21; docs/devices.md).

The other device tests call the handler methods with a stub for the socket. This one starts the actual
ThreadingHTTPServer with the real Handler and talks to it with urllib, exactly the way the Android app and the
extension do -- headers, Content-Length, JSON, the long poll -- so the wire format the apps were built against
is what is verified, not just the Python behind it. Only the model is faked."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_devhttp_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_devhttp_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import bridge  # noqa: E402
import config  # noqa: E402
import conversation  # noqa: E402
import device_data  # noqa: E402
import devices  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Tester", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]

_httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
_httpd.daemon_threads = True
BASE = f"http://127.0.0.1:{_httpd.server_address[1]}"
threading.Thread(target=_httpd.serve_forever, daemon=True).start()


def call(path, body=None, token=None, timeout=30, method="POST"):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def _reset():
    for tbl in ("bridge_commands", "bridge_tokens", "device_pairing_codes", "places", "place_events", "place_reminders",
                "device_health", "browser_day", "device_activity", "messages", "tracker_entries", "tracker_types"):
        store.write(lambda c, t=tbl: c.execute(f"DELETE FROM {t}"))
    for k, spec in config._SPEC.items():
        if k.startswith(("phone_", "browser_")) or k == "bridge_standing_look":
            config.set("user", UID, k, spec[0])
    config.set("user", UID, "bridge_session_until", 0)
    config.set("workspace", WS, "bridge_enabled", False)
    accounts._LOGIN_FAILS.clear()
    bridge._suspicious_until.clear()


def _pair_over_http(kind):
    code, _ = devices.create_code(UID, WS, kind)
    path = "/device/register" if kind == "phone" else "/bridge/register"
    status, body = call(path, {"code": code, "name": f"test {kind}", "v": "0.1.0"})
    assert status == 200, (status, body)
    return body["token"]


class PhoneOverHttp(unittest.TestCase):
    def setUp(self):
        _reset()
        p = patch.object(devices, "in_quiet_hours", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        self.token = _pair_over_http("phone")

    def test_a_reminder_arrives_on_the_long_poll_and_is_acked(self):
        mid = conversation.add_message(UID, "assistant", "Take the bins out", kind="reminder")
        status, body = call("/device/poll", {"v": "0.1.0", "caps": {"notifications": True}}, self.token, timeout=40)
        self.assertEqual(status, 200)
        self.assertEqual([(m["id"], m["category"], m["text"]) for m in body["messages"]], [(mid, "reminder", "Take the bins out")])
        with patch.object(devices, "PHONE_POLL_WAIT_S", 0.2):
            status, body = call("/device/poll", {"ack": mid, "caps": {"notifications": True}}, self.token)
        self.assertEqual(body["messages"], [])

    def test_a_held_poll_returns_the_moment_a_message_lands(self):
        out = {}

        def go():
            out["r"] = call("/device/poll", {"caps": {"notifications": True}}, self.token, timeout=40)
        t = threading.Thread(target=go)
        t0 = time.time()
        t.start()
        time.sleep(0.8)
        mid = conversation.add_message(UID, "assistant", "ping", kind="proactive")
        t.join(10)
        self.assertEqual(out["r"][0], 200)
        self.assertEqual([m["id"] for m in out["r"][1]["messages"]], [mid])
        self.assertLess(time.time() - t0, 6, "delivered within a tick of the message, not at the end of the 25 s hold")

    def test_a_phone_command_round_trip_over_the_wire(self):
        config.set("user", UID, "phone_actions_enabled", True)
        with patch.object(devices, "PHONE_POLL_WAIT_S", 0.2):
            call("/device/poll", {"caps": {"notifications": True, "actions": True}}, self.token)
        result = {}

        def run_tool():
            import tools
            import phone_tools  # noqa: F401
            result["r"] = tools.dispatch("phone_open_link", {"url": "https://example.com/x", "why": "test"},
                                         {"user_id": UID, "workspace_id": WS, "role": "admin"})
        t = threading.Thread(target=run_tool)
        t.start()
        got = None
        for _ in range(40):
            with patch.object(devices, "PHONE_POLL_WAIT_S", 0.3):
                status, body = call("/device/poll", {"caps": {"notifications": True, "actions": True}}, self.token)
            if body.get("command"):
                got = body["command"]
                break
        self.assertIsNotNone(got, "the phone received the command")
        self.assertEqual((got["action"], got["args"]), ("open_url", {"url": "https://example.com/x"}))
        self.assertEqual(call("/device/ack", {"id": got["id"]}, self.token), (200, {"ok": True}))
        self.assertEqual(call("/device/result", {"id": got["id"], "ok": True, "result": {"ok": True}}, self.token)[0], 200)
        t.join(10)
        self.assertTrue(result["r"]["ok"], result["r"])

    def test_the_apk_download_over_http(self):
        d = devices.apk_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "nori-companion.apk").write_bytes(b"PK\x03\x04" + b"x" * 5000)
        (d / "version.txt").write_text("0.1.0")
        try:
            status, body = call("/device/app.apk", token=self.token, method="GET")
            self.assertEqual((status, len(body)), (200, 5004))
            self.assertEqual(call("/device/app.apk", method="GET")[0], 401)
            with patch.object(devices, "PHONE_POLL_WAIT_S", 0.1):
                self.assertEqual(call("/device/poll", {}, self.token)[1]["app"], {"latest": "0.1.0", "apk_url": "/device/app.apk"})
        finally:
            (d / "nori-companion.apk").unlink()
            (d / "version.txt").unlink()

    def test_errors_have_the_status_codes_the_app_acts_on(self):
        self.assertEqual(call("/device/poll", {}, "npn_forged")[0], 401)
        self.assertEqual(call("/device/register", {"code": "AAAA-AAAA"})[0], 401)
        self.assertEqual(call("/device/poll", {}, self.token_for_other_kind())[0], 401)
        req = urllib.request.Request(BASE + "/device/poll", data=b"{not json", method="POST",
                                     headers={"Authorization": "Bearer " + self.token})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 400)
        big = urllib.request.Request(BASE + "/device/poll", data=b"x" * 20000, method="POST",
                                     headers={"Authorization": "Bearer " + self.token})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(big, timeout=10)
        self.assertEqual(cm.exception.code, 413)

    def token_for_other_kind(self):
        return _pair_over_http("browser")

    def test_unregister_makes_the_token_dead(self):
        self.assertEqual(call("/device/unregister", {}, self.token), (200, {"ok": True}))
        self.assertEqual(call("/device/poll", {}, self.token)[0], 401)


class BrowserOverHttp(unittest.TestCase):
    def setUp(self):
        _reset()
        p = patch.object(devices, "in_quiet_hours", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        self.token = _pair_over_http("browser")
        device_data.set_turn_runner(lambda uid, extra=None: {"ok": True, "messages": [
            {"id": 9, "role": "assistant", "kind": "chat", "content": "here you go"}]})
        self.addCleanup(device_data.set_turn_runner, None)

    def test_poll_share_ask_activity_and_the_approval_buttons(self):
        mid = conversation.add_message(UID, "assistant", "Dinner is ready", kind="reminder")
        with patch.object(bridge, "INACTIVE_POLL_WAIT_S", 0.2):
            status, body = call("/bridge/poll", {"ext": {"v": "0.2.0"}, "tab_visible": False}, self.token)
        self.assertEqual(status, 200)
        self.assertEqual([(m["id"], m["category"]) for m in body["messages"]], [(mid, "reminder")])
        self.assertEqual(body["settings"]["notifications"], True)
        status, body = call("/bridge/share", {"kind": "link", "dest": "note", "url": "https://example.com/a", "title": "A page"}, self.token)
        self.assertEqual((status, body["ok"]), (200, True))
        with patch("ingest.summarize_untrusted", return_value={"content": "PAGE", "suspicious": False}):
            status, body = call("/bridge/ask", {"question": "what is this", "url": "https://example.com", "title": "T",
                                                "text": "page text " * 30}, self.token)
        self.assertEqual((status, body), (200, {"ok": True, "reply": "here you go"}))
        config.set("user", UID, "browser_activity_enabled", True)
        status, body = call("/bridge/activity", {"host_seconds": [{"host": "example.com", "seconds": 45}]}, self.token)
        self.assertEqual((status, body["recorded_seconds"]), (200, 45))
        config.set("workspace", WS, "bridge_enabled", True)
        bridge.start_session(UID, 15)
        cid = bridge._enqueue(UID, "click", {"tab_id": 1, "ref": "e1"}, summary="click Send", host="x.com", source="chat")
        store.write(lambda c: c.execute("UPDATE bridge_commands SET status='awaiting_approval', approval='required' WHERE id=?", (cid,)))
        status, body = call("/bridge/approval", {"id": cid, "decision": "deny"}, self.token)
        self.assertEqual((status, body["status"]), (200, "denied"))

    def test_the_bearer_approval_route_and_the_session_chat_card_route_coexist(self):
        # Without a bearer header, /bridge/approval is the chat card's session+CSRF route, not the extension's:
        # an anonymous caller is sent to the login page (303), not answered with the bearer route's 401 JSON.
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", _httpd.server_address[1], timeout=10)
        conn.request("POST", "/bridge/approval", body="id=1&decision=deny",
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        r = conn.getresponse()
        r.read()
        conn.close()
        self.assertEqual((r.status, r.getheader("Location")), (303, "/login"))

    def test_a_command_reaches_the_extension_with_its_allowlist_and_the_result_goes_back(self):
        config.set("workspace", WS, "bridge_enabled", True)
        bridge.add_rule("*.example.com")
        bridge.start_session(UID, 15)
        cid = bridge._enqueue(UID, "tabs", {}, summary="list tabs", host=None, source="chat")
        status, body = call("/bridge/poll", {"ext": {"v": "0.2.0"}}, self.token, timeout=40)
        self.assertEqual(status, 200)
        self.assertEqual((body["command"]["id"], body["command"]["allowed"], body["command"]["look_only"]),
                         (cid, ["*.example.com"], False))
        status, body = call("/bridge/result", {"id": cid, "ok": True, "result": {"tabs": [], "hidden": 0}}, self.token)
        self.assertEqual((status, body["ok"]), (200, True))
        self.assertEqual(bridge._cmd(cid)["status"], "done")


if __name__ == "__main__":
    unittest.main()

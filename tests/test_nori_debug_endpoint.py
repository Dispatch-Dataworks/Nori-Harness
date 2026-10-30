# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The debug endpoint (debug_api.py, 2026-10-30). It exposes private conversations to anyone holding a token, so what is pinned here is mostly "no":
off by default and indistinguishable from a missing page; no access without the exact token (never from the URL); no secret ever in any section, planted
in every place one could live; unreachable by the agent's own tools; admin-only to configure; every request recorded. Then that each section really
shows what a developer needs. Real server, real SQLite."""
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_debug_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_debug_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"
os.environ["NORI_SECRET_ENV_TEST"] = "env-secret-value-should-not-show"

import accounts  # noqa: E402
import config  # noqa: E402
import conversation  # noqa: E402
import crypto  # noqa: E402
import debug_api  # noqa: E402
import logbuf  # noqa: E402
import peers  # noqa: E402
import providers  # noqa: E402
import server  # noqa: E402
import settings_tool  # noqa: E402
import store  # noqa: E402
import tools  # noqa: E402

store.init()
logbuf.install()          # as server.main() does: keep a copy of what is printed
_admin = accounts.bootstrap_admin("Tester", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
config.set("workspace", WS, "setup_complete", True)
config.set("workspace", WS, "assistant_name", "Sage")
_inv, _raw = accounts.create_invite(WS, "Claire", "member", UID)
MEMBER = accounts.accept_invite(_raw, "memberpass123")
ADMIN_TOKEN = accounts.new_session(_admin, "127.0.0.1")
MEMBER_TOKEN = accounts.new_session(MEMBER, "127.0.0.1")
CSRF = accounts.get_session(ADMIN_TOKEN)["csrf"]
MEMBER_CSRF = accounts.get_session(MEMBER_TOKEN)["csrf"]
SESS = accounts.get_session(ADMIN_TOKEN)

DEBUG_TOKEN = "dbg-" + "A1b2C3d4" * 4              # 36 characters
WRONG_TOKEN = "dbg-" + "Z9y8X7w6" * 4

# ── plant a secret in every place one could live ─────────────────────────
S_PROVIDER = "sk-or-v1-PLANTEDPROVIDERKEY0123456789abcdef"
S_MCP = "PLANTEDMCPTOKEN0123456789"
S_NODRYA = "https://nodrya.example.com/mcp?token=PLANTEDNODRYATOKEN012345"
S_DEVICE = "PLANTEDDEVICETOKENHASH0123456789abcdef0123456789"
S_LOG = "sk-ant-api03-PLANTEDLOGKEY0123456789"
S_BEARER = "PLANTEDBEARERVALUE0123456789"
S_CONTENT = "ghp_PLANTEDCHATGITHUBTOKEN0123456789"
S_PSK = "PLANTEDPSKVALUE0123456789abcdef"
SECRETS = ["PLANTEDINVITEURLKEY0123", S_PROVIDER, S_MCP, "PLANTEDNODRYATOKEN012345", S_DEVICE, S_LOG, S_BEARER, S_CONTENT, S_PSK, DEBUG_TOKEN,
           ADMIN_TOKEN, MEMBER_TOKEN, os.environ["NORI_SECRET_ENV_TEST"], "testpass123", "memberpass123"]

providers.create_api_key(WS, "openrouter", "OpenRouter", S_PROVIDER, UID)
store.write(lambda c: c.execute(
    "INSERT INTO mcp_servers(scope, scope_id, name, url, auth_type, credential_enc, purpose, enabled, created_ts, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
    ("workspace", WS, "notes", f"https://mcp.example.com/sse?token={S_MCP}", "bearer", crypto.encrypt(S_MCP), "x", 1, time.time(), UID)))
config.set("workspace", WS, "nodrya_mcp_url", S_NODRYA)
store.write(lambda c: c.execute(
    "INSERT INTO bridge_tokens(user_id, workspace_id, label, token_hash, created_ts, kind, caps, state) VALUES (?,?,?,?,?,?,?,?)",
    (UID, WS, "Pixel", S_DEVICE, time.time(), "phone", "{}", "ok")))
peers.renegotiate_now = lambda peer_id: None
_a = peers.create_invite(SESS, address="https://claire.example.com/base")["peer_id"]
_b = peers.create_invite(SESS, address="https://nori.example.com")["peer_id"]
peers.accept_connection_string(SESS, _a, peers.connection_string(SESS, _b), name="Claire", purpose="household")
PEER = _a
store.write(lambda c: c.execute("UPDATE peers SET psk_enc=?, url=? WHERE id=?",
                                (crypto.encrypt(S_PSK), "https://claire.example.com/base?apikey=PLANTEDINVITEURLKEY0123", PEER)))
store.write(lambda c: c.execute(
    "INSERT INTO peer_messages(peer_id, conversation_id, message_id, direction, type, seq, body_json, ts, status, last_error) VALUES (?,?,?,?,?,?,?,?,?,?)",
    (PEER, "c1", "m1", "received", "message", 1,
     json.dumps({"content": "can we do lunch?", "category": "suspicious", "suspicious": True, "screening_failed": True,
                 "summary": "(could not be read safely -- review the original directly)"}), time.time(), "delivered", "screen blew up")))
conversation.add_message(UID, "user", "hello there, my key is " + S_CONTENT)
conversation.add_message(UID, "assistant", "hi!", emotion="happy", meta={"debug": {"model_alias": "Sage", "latency_ms": 812, "cut_off": True, "finish_reason": "length"},
                                                                         "prompt_tokens": 120, "completion_tokens": 33, "cost_usd": 0.0012})
conversation.add_message(UID, "assistant", "used remember", kind="tool", meta={"tool_name": "remember", "failed": True, "error": "boom"})
conversation.add_message(MEMBER["id"], "user", "claire's private message")

_httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
_httpd.daemon_threads = True
BASE = f"http://127.0.0.1:{_httpd.server_address[1]}"
threading.Thread(target=_httpd.serve_forever, daemon=True).start()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def req(path, *, method="GET", headers=None, form=None):
    data = None if form is None else urllib.parse.urlencode(form).encode()
    r = urllib.request.Request(BASE + path, data=data, method=method if form is None else "POST", headers=headers or {})
    try:
        with _opener.open(r, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers


def MISSING():
    """What any path that does not exist gets from a signed-out visitor (a redirect to the login page)."""
    return req("/definitely-not-a-page")[0]


def dbg(path, token=DEBUG_TOKEN):
    return req(path, headers={"Authorization": f"Bearer {token}"} if token else {})


def js(path, token=DEBUG_TOKEN):
    status, body, _ = dbg(path, token)
    return status, json.loads(body)


def admin(path, form=None, token=ADMIN_TOKEN, csrf=CSRF):
    return req(path, headers={"Cookie": f"ns={token}"}, form=None if form is None else {"csrf": csrf, **form})


def turn_on(hours=0):
    assert debug_api.set_token(WS, DEBUG_TOKEN)[0]
    assert debug_api.set_enabled(WS, True, hours)[0]


def turn_off():
    debug_api.set_enabled(WS, False)
    debug_api.clear_token(WS)
    accounts._LOGIN_FAILS.clear()


def _reset():
    turn_off()
    store.write(lambda c: c.execute("DELETE FROM debug_access_log"))
    logbuf.clear()


class OffByDefault(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_a_fresh_instance_has_it_off_and_no_token(self):
        self.assertFalse(config._SPEC["debug_enabled"][0])
        self.assertFalse(debug_api.is_enabled(WS))
        self.assertEqual(debug_api.fingerprint(WS), "")

    def test_off_is_indistinguishable_from_a_page_that_does_not_exist(self):
        for headers in ({}, {"Authorization": f"Bearer {DEBUG_TOKEN}"}, {"Cookie": f"ns={ADMIN_TOKEN}"}):
            for path, other in (("/debug", "/definitely-not-a-page"), ("/debug/overview", "/definitely/not/a-page"), ("/debug/conversation?user=x", "/nope?user=x")):
                off = req(path, headers=headers)
                missing = req(other, headers=headers)
                self.assertEqual((off[0], off[1]), (missing[0], missing[1]), f"{path} with {sorted(headers)}")

    def test_the_right_token_still_gets_nothing_while_it_is_off(self):
        debug_api.set_token(WS, DEBUG_TOKEN)                  # a token exists but the switch was never turned on
        self.assertEqual(dbg("/debug")[0], MISSING())
        self.assertEqual(dbg("/debug/conversation")[0], MISSING())

    def test_turning_it_on_needs_a_token_first(self):
        ok, msg = debug_api.set_enabled(WS, True, 0)
        self.assertFalse(ok)
        self.assertIn("token first", msg)
        self.assertFalse(debug_api.is_enabled(WS))

    def test_the_timer_switches_it_off_for_good(self):
        turn_on(hours=1)
        self.assertEqual(dbg("/debug")[0], 200)
        config.set("workspace", WS, "debug_until_ts", time.time() - 1)
        self.assertEqual(dbg("/debug")[0], MISSING())
        self.assertFalse(config.get("workspace", WS, "debug_enabled"), "a lapsed timer clears the switch, it does not come back by itself")
        self.assertEqual(config.get("workspace", WS, "debug_until_ts"), 0.0)

    def test_a_timer_is_set_from_the_hours(self):
        debug_api.set_token(WS, DEBUG_TOKEN)
        debug_api.set_enabled(WS, True, 2)
        self.assertAlmostEqual(config.get("workspace", WS, "debug_until_ts"), time.time() + 7200, delta=5)
        debug_api.set_enabled(WS, True, 0)
        self.assertEqual(config.get("workspace", WS, "debug_until_ts"), 0.0)


class TheToken(unittest.TestCase):
    def setUp(self):
        _reset()
        turn_on()

    def test_no_token_or_the_wrong_one_is_refused(self):
        self.assertEqual(dbg("/debug/overview", token=None)[0], 401)
        self.assertEqual(dbg("/debug/overview", token=WRONG_TOKEN)[0], 401)
        self.assertEqual(dbg("/debug/overview", token=DEBUG_TOKEN[:-1])[0], 401)
        self.assertEqual(dbg("/debug/overview", token=DEBUG_TOKEN + "x")[0], 401)
        self.assertEqual(dbg("/debug/overview")[0], 200)

    def test_a_token_in_the_url_is_never_accepted(self):
        for q in (f"?token={DEBUG_TOKEN}", f"?access_token={DEBUG_TOKEN}", f"?key={DEBUG_TOKEN}"):
            self.assertEqual(req("/debug/overview" + q)[0], 401, q)

    def test_a_session_cookie_is_not_a_debug_token(self):
        self.assertEqual(req("/debug/overview", headers={"Cookie": f"ns={ADMIN_TOKEN}"})[0], 401)
        self.assertEqual(req("/debug/overview", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})[0], 401)

    def test_failures_are_rate_limited_like_the_login_form(self):
        for _ in range(accounts.MAX_FAILS):
            self.assertEqual(dbg("/debug", token=WRONG_TOKEN)[0], 401)
        self.assertEqual(dbg("/debug", token=WRONG_TOKEN)[0], 429)
        self.assertEqual(dbg("/debug")[0], 429, "even the right token waits once the address is locked out")
        accounts._LOGIN_FAILS.clear()
        self.assertEqual(dbg("/debug")[0], 200)

    def test_only_the_hash_is_stored(self):
        stored = config.get("workspace", WS, "debug_token_hash")
        self.assertEqual(stored, hashlib.sha256(DEBUG_TOKEN.encode()).hexdigest())
        rows = store.read(lambda c: [tuple(r) for r in c.execute("SELECT * FROM settings").fetchall()])
        self.assertFalse([r for r in rows if DEBUG_TOKEN in " ".join(map(str, r))], "the token itself is nowhere in the settings table")

    def test_weak_tokens_are_refused(self):
        for bad in ("short", "x" * 23, "has space in it " * 3, "x" * 201):
            ok, msg = debug_api.set_token(WS, bad)
            self.assertFalse(ok, bad)
            self.assertTrue(msg)
        self.assertEqual(debug_api.fingerprint(WS), hashlib.sha256(DEBUG_TOKEN.encode()).hexdigest()[:8], "a refused token changes nothing")

    def test_the_same_token_gives_the_same_fingerprint_on_every_agent(self):
        self.assertEqual(debug_api.fingerprint(WS), hashlib.sha256(DEBUG_TOKEN.encode()).hexdigest()[:8])

    def test_replacing_the_token_locks_the_old_one_out(self):
        debug_api.set_token(WS, WRONG_TOKEN)
        self.assertEqual(dbg("/debug")[0], 401)
        self.assertEqual(dbg("/debug", token=WRONG_TOKEN)[0], 200)

    def test_removing_the_token_switches_it_off(self):
        debug_api.clear_token(WS)
        self.assertEqual(dbg("/debug")[0], MISSING())
        self.assertFalse(config.get("workspace", WS, "debug_enabled"))

    def test_only_get_is_answered(self):
        status, _, _ = req("/debug/overview", method="POST", form={"x": "1"}, headers={"Authorization": f"Bearer {DEBUG_TOKEN}"})
        self.assertNotEqual(status, 200)


class NotReachableByTheAgent(unittest.TestCase):
    def setUp(self):
        _reset()
        turn_on()

    def test_the_three_settings_are_secret_so_no_tool_can_read_them(self):
        for k in ("debug_enabled", "debug_until_ts", "debug_token_hash"):
            self.assertTrue(config._SPEC[k][3], k)
            self.assertNotIn(k, config.readable_spec())
        out = settings_tool._get_settings_impl(SESS)["settings"]
        self.assertFalse([k for k in out if k in ("debug_enabled", "debug_until_ts", "debug_token_hash")])

    def test_update_settings_cannot_change_them(self):
        for k in ("debug_enabled", "debug_until_ts", "debug_token_hash"):
            self.assertNotIn(k, settings_tool.PEER_SETTINGS_KEYS)
            res = tools.dispatch("update_settings", {"field": k, "value": True}, SESS)
            self.assertIn("error", res, k)
        turn_off()
        self.assertFalse(config.get("workspace", WS, "debug_enabled"))

    def test_no_tool_mentions_the_debug_endpoint(self):
        for name, t in tools._REGISTRY.items():
            self.assertNotIn("debug_api", repr(t.impl), name)


class NoSecretsAnywhere(unittest.TestCase):
    """Every section, with a secret planted in every place one could live: none may come back."""

    def setUp(self):
        _reset()
        turn_on()
        print(f"calling the api with Authorization: Bearer {S_BEARER} and key {S_LOG}", flush=True)
        print(f"retrying https://x.example.com/?api_key={S_BEARER}", flush=True)
        logbuf.add("err", f"error: connection refused for token={S_BEARER}")

    def _everything(self):
        out = {}
        for name in debug_api.SECTIONS:
            status, body, _ = dbg(f"/debug/{name}")
            self.assertEqual(status, 200, name)
            out[name] = body
        for extra in ("/debug/conversation?user=Sage", "/debug/conversation?user=Claire", "/debug/logs?lines=4000", "/debug/errors?limit=300",
                      "/debug/peers?messages=100", "/debug/settings", "/debug/turns?limit=200"):
            out[extra] = dbg(extra)[2] and dbg(extra)[1]
        return out

    def test_no_planted_secret_appears_in_any_section(self):
        for name, body in self._everything().items():
            for secret in SECRETS:
                self.assertNotIn(secret, body, f"{name} leaked {secret[:14]}...")

    def test_the_scrubber_masks_by_name_and_by_look(self):
        out = debug_api.scrub({"api_key": "abc", "psk_enc": "zzz", "x": {"access_token": "t1"}, "prompt_tokens": 12, "completion_tokens": 3,
                               "text": f"Authorization: Bearer {S_BEARER} and {S_LOG} and paci1:AAAAAAAAAAAAAAAAAAAA and ns={'a' * 24}",
                               "empty_key": "", "token_hash": "h"})
        self.assertEqual(out["api_key"], "[redacted]")
        self.assertEqual(out["psk_enc"], "[redacted]")
        self.assertEqual(out["x"]["access_token"], "[redacted]")
        self.assertEqual(out["token_hash"], "[redacted]")
        self.assertEqual((out["prompt_tokens"], out["completion_tokens"]), (12, 3), "token COUNTS are diagnostics, not secrets")
        for s in (S_BEARER, S_LOG, "AAAAAAAAAAAAAAAAAAAA", "a" * 24):
            self.assertNotIn(s, out["text"])

    def test_urls_lose_credentials_and_query_strings(self):
        self.assertEqual(debug_api.safe_url("https://user:pw@h.example.com:8443/p/q?token=abc#f"), "https://h.example.com:8443/p/q")
        self.assertEqual(debug_api.safe_url("not a url"), "not a url")

    def test_settings_show_what_differs_from_default_without_secrets(self):
        config.set("workspace", WS, "max_reply_tokens", 9000)
        data = js("/debug/settings")[1]["data"]
        self.assertIn("max_reply_tokens", data["changed_from_default"]["workspace"])
        self.assertEqual(data["workspace"]["max_reply_tokens"]["value"], 9000)
        self.assertNotIn("debug_enabled", data["workspace"])
        self.assertNotIn("nodrya_mcp_url", data["workspace"])
        self.assertIn("NORI_SECRET_ENV_TEST", data["environment_set_but_not_shown"])
        self.assertNotIn("NORI_SECRET_ENV_TEST", data["environment"])


class WhatItShows(unittest.TestCase):
    def setUp(self):
        _reset()
        turn_on()

    def test_the_index_lists_every_section(self):
        status, body = js("/debug")
        self.assertEqual(status, 200)
        self.assertEqual(set(body["sections"]), set(debug_api.SECTIONS))
        self.assertEqual(js("/debug/")[0], 200)

    def test_an_unknown_section_is_404_with_the_list(self):
        status, body = js("/debug/nope")
        self.assertEqual(status, 404)
        self.assertIn("overview", body["sections"])

    def test_every_section_is_json_with_an_envelope_and_never_cached(self):
        for name in debug_api.SECTIONS:
            status, raw, headers = dbg(f"/debug/{name}")
            body = json.loads(raw)
            self.assertEqual((status, body["ok"], body["section"], body["assistant"]), (200, True, name, "Sage"), name)
            self.assertIn("data", body)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertNotIn("error", body["data"] if isinstance(body["data"], dict) and len(body["data"]) == 1 else {}, name)

    def test_overview(self):
        d = js("/debug/overview")[1]["data"]
        self.assertEqual(d["assistant_name"], "Sage")
        self.assertEqual({u["display_name"] for u in d["users"]}, {"Tester", "Claire"})
        self.assertGreaterEqual(d["messages"]["total"], 4)
        self.assertIn("NORI_SECRET_ENV_TEST", d["env_set"], "names only")
        self.assertEqual([p["type"] for p in d["providers"]], ["openrouter"])
        self.assertTrue(d["version"])
        self.assertGreaterEqual(d["uptime_s"], 0)

    def test_health(self):
        d = js("/debug/health")[1]["data"]
        for k in ("integrations", "peers_paci", "devices", "scheduler", "threads", "updates", "sandbox", "mcp_servers", "providers"):
            self.assertIn(k, d, k)
        self.assertEqual(d["devices"][0]["label"], "Pixel")
        self.assertNotIn("token_hash", d["devices"][0])
        self.assertEqual(d["mcp_servers"][0]["url"], "https://mcp.example.com/sse")
        self.assertEqual(d["peers_paci"][0]["url"], "https://claire.example.com/base")

    def test_the_scheduler_records_what_its_last_cycle_did_and_its_failures(self):
        import scheduler
        self.assertIn("cycles", scheduler.LAST_CYCLE)
        self.assertIn("error", js("/debug/health")[1]["data"]["scheduler"])

    def test_conversation_by_user_name_and_id(self):
        by_name = js("/debug/conversation?user=Tester&limit=10")[1]["data"]
        by_id = js(f"/debug/conversation?user={UID}&limit=10")[1]["data"]
        self.assertEqual([m["id"] for m in by_name["messages"]], [m["id"] for m in by_id["messages"]])
        texts = [m["content"] for m in by_name["messages"]]
        self.assertTrue(any(t.startswith("hello there") for t in texts))
        self.assertNotIn("claire's private message", " ".join(texts), "one user's rows never appear under another")
        asst = next(m for m in by_name["messages"] if m["content"] == "hi!")
        self.assertEqual(asst["emotion"], "happy")
        self.assertEqual(asst["meta"]["debug"]["finish_reason"], "length")
        self.assertEqual(asst["meta"]["prompt_tokens"], 120)
        claire = js("/debug/conversation?user=Claire")[1]["data"]
        self.assertEqual([m["content"] for m in claire["messages"]], ["claire's private message"])

    def test_conversation_paging_and_filters(self):
        full = js("/debug/conversation?user=Tester&limit=100")[1]["data"]
        ids = [m["id"] for m in full["messages"]]
        page = js(f"/debug/conversation?user=Tester&limit=1&before={ids[-1]}")[1]["data"]
        self.assertEqual([m["id"] for m in page["messages"]], [ids[-2]])
        self.assertEqual(js("/debug/conversation?user=Tester&kinds=tool")[1]["data"]["count"], 1)
        self.assertEqual(js("/debug/conversation?user=Tester&grep=hello")[1]["data"]["count"], 1)
        self.assertEqual(js("/debug/conversation?user=nobody")[1]["data"]["error"], "no such user")
        self.assertLessEqual(js("/debug/conversation?user=Tester&limit=99999")[1]["data"]["count"], 300)

    def test_turns_show_how_a_reply_was_made(self):
        t = js("/debug/turns?user=Tester")[1]["data"]["turns"][0]
        self.assertEqual(t["preview"], "hi!")
        self.assertEqual(t["meta"]["debug"]["latency_ms"], 812)
        self.assertTrue(t["meta"]["debug"]["cut_off"])

    def test_errors_gathers_the_places_a_developer_would_look(self):
        print("ingest: SCREENING FAILED for a PACI message -- recorded as suspicious, not judged: ModelError: no key", flush=True)
        print("chat.run round 1: REPLY CUT OFF at the 4096-token limit (finish_reason='length')", flush=True)
        d = js("/debug/errors")[1]["data"]
        self.assertEqual(d["failed_tool_calls"][0]["meta"]["error"], "boom")
        self.assertGreaterEqual(d["log_error_counts"]["screening_failed"], 1)
        self.assertGreaterEqual(d["log_error_counts"]["reply_cut_off"], 1)
        self.assertTrue(any("SCREENING FAILED" in r["text"] for r in d["log_errors"]))
        self.assertEqual(d["peer_message_problems"][0]["last_error"], "screen blew up")
        for k in ("events", "job_problems", "peer_model_failures", "schedule_failures", "image_failures"):
            self.assertIn(k, d, k)

    def test_logs_filter_by_text_level_and_count(self):
        print("a perfectly ordinary line", flush=True)
        print("something FAILED badly", flush=True)
        d = js("/debug/logs?grep=ordinary")[1]["data"]
        self.assertTrue(all("ordinary" in r["text"] for r in d["lines"]) and d["count"] >= 1)
        self.assertTrue(all(r["level"] == "error" for r in js("/debug/logs?level=error")[1]["data"]["lines"]))
        self.assertLessEqual(js("/debug/logs?lines=3")[1]["data"]["count"], 3)

    def test_peers_show_screening_results_for_each_message(self):
        d = js("/debug/peers")[1]["data"]["peers"][0]
        self.assertEqual(d["name"], "Claire")
        m = d["recent_messages"][0]
        self.assertTrue(m["screening"]["screening_failed"], "a developer can tell 'could not screen' from 'screened and flagged'")
        self.assertEqual(m["content"], "can we do lunch?")
        self.assertNotIn("psk_enc", d)

    def test_memory_jobs_schedules_pings_tools_db(self):
        self.assertIn("counts", js("/debug/memory")[1]["data"])
        self.assertIn("jobs", js("/debug/jobs")[1]["data"])
        self.assertIn("schedules", js("/debug/schedules")[1]["data"])
        self.assertIn("signals", js("/debug/pings")[1]["data"])
        t = js("/debug/tools")[1]["data"]
        self.assertGreater(t["count"], 10)
        self.assertIn("update_settings", {x["name"] for x in t["tools"]})
        self.assertGreater(js("/debug/db")[1]["data"]["tables"]["messages"], 0)

    def test_a_section_that_breaks_says_so_instead_of_blanking_the_response(self):
        real = debug_api.SECTIONS["db"]
        debug_api.SECTIONS["db"] = (lambda p: 1 / 0, "x")
        try:
            status, body = js("/debug/db")
        finally:
            debug_api.SECTIONS["db"] = real
        self.assertEqual(status, 200)
        self.assertIn("ZeroDivisionError", body["data"]["error"])

    def test_an_oversized_answer_is_refused_with_advice(self):
        real = debug_api.MAX_RESPONSE_BYTES
        debug_api.MAX_RESPONSE_BYTES = 200
        try:
            status, body = js("/debug/overview")
        finally:
            debug_api.MAX_RESPONSE_BYTES = real
        self.assertEqual(status, 413)
        self.assertIn("ask for less", body["error"])


class TheLogRing(unittest.TestCase):
    def setUp(self):
        logbuf.clear()

    def test_it_keeps_complete_lines_levels_and_a_bounded_number(self):
        logbuf.add("out", "all fine")
        logbuf.add("err", "Traceback (most recent call last):")
        got = logbuf.recent()
        self.assertEqual([r["level"] for r in got], ["info", "error"])
        for i in range(logbuf.MAX_LINES + 50):
            logbuf.add("out", f"line {i}")
        self.assertEqual(len(logbuf._lines), logbuf.MAX_LINES)
        self.assertEqual(logbuf.recent(1)[0]["text"], f"line {logbuf.MAX_LINES + 49}")

    def test_the_tee_passes_everything_through_and_keeps_a_copy(self):
        sink = io.StringIO()
        tee = logbuf._Tee(sink, "out")
        tee.write("partial ")
        tee.write("line\nsecond\n")
        self.assertEqual(sink.getvalue(), "partial line\nsecond\n")
        self.assertEqual([r["text"] for r in logbuf.recent()], ["partial line", "second"])
        tee.flush()
        self.assertTrue(hasattr(tee, "getvalue"), "unknown attributes fall through to the real stream")

    def test_an_overlong_line_is_capped(self):
        logbuf.add("out", "x" * 99999)
        self.assertLessEqual(len(logbuf.recent(1)[0]["text"]), logbuf.MAX_LINE_CHARS)


class TheAdminTab(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_the_tab_is_admin_only(self):
        self.assertEqual(admin("/settings?tab=debug")[0], 200)
        status, body, _ = admin("/settings?tab=debug", token=MEMBER_TOKEN, csrf=MEMBER_CSRF)
        self.assertNotIn("fingerprint", body, "a member sees none of it")
        self.assertNotIn("<h2>debug endpoint</h2>", body)
        status, body, _ = admin("/admin/debug/switch", {"on": "1"}, token=MEMBER_TOKEN, csrf=MEMBER_CSRF)
        self.assertEqual(status, 403)
        self.assertFalse(config.get("workspace", WS, "debug_enabled"))

    def test_it_says_off_and_never_shows_a_token(self):
        page = admin("/settings?tab=debug")[1]
        self.assertIn("does not exist", page)
        self.assertIn("not set", page)
        self.assertNotIn("<input type=password name=token autocomplete=off required minlength=24 spellcheck=false value", page)

    def test_the_whole_flow_through_the_forms(self):
        status, page, _ = admin("/admin/debug/token", {"token": "tooshort"})
        self.assertIn("at least 24 characters", page)
        status, page, _ = admin("/admin/debug/switch", {"on": "1", "hours": "0"})
        self.assertIn("set a token first", page)
        status, page, _ = admin("/admin/debug/token", {"token": DEBUG_TOKEN})
        self.assertIn("fingerprint " + debug_api.fingerprint(WS), page)
        self.assertNotIn(DEBUG_TOKEN, page, "the token is never echoed back")
        self.assertEqual(dbg("/debug")[0], MISSING(), "a token alone does not turn it on")
        status, page, _ = admin("/admin/debug/switch", {"on": "1", "hours": "8"})
        self.assertIn("debug endpoint on for 8 hour(s)", page)
        self.assertIn("switches itself off", page)
        self.assertEqual(dbg("/debug")[0], 200)
        self.assertIn("debug on", admin("/")[1], "a chip in the header while it is on")
        status, page, _ = admin("/admin/debug/switch", {"on": "0"})
        self.assertIn("debug endpoint off", page)
        self.assertEqual(dbg("/debug")[0], MISSING())
        self.assertNotIn("debug on</a>", admin("/")[1])

    def test_removing_the_token(self):
        turn_on()
        status, page, _ = admin("/admin/debug/clear", {})
        self.assertIn("token removed", page)
        self.assertEqual(dbg("/debug")[0], MISSING())
        self.assertEqual(debug_api.fingerprint(WS), "")

    def test_the_forms_need_the_csrf_token(self):
        status, _, _ = req("/admin/debug/switch", headers={"Cookie": f"ns={ADMIN_TOKEN}"}, form={"on": "1"})
        self.assertEqual(status, 403)
        self.assertFalse(config.get("workspace", WS, "debug_enabled"))

    def test_the_header_chip_is_for_admins_only(self):
        turn_on()
        self.assertNotIn("debug on", admin("/", token=MEMBER_TOKEN, csrf=MEMBER_CSRF)[1])


class TheAccessRecord(unittest.TestCase):
    def setUp(self):
        _reset()
        turn_on()

    def test_every_request_is_recorded_with_its_outcome(self):
        dbg("/debug/overview")
        dbg("/debug/overview", token=WRONG_TOKEN)
        dbg("/debug/nope")
        rows = debug_api.recent_access(10)
        self.assertEqual([(r["status"], r["path"]) for r in rows], [(404, "/debug/nope"), (401, "/debug/overview"), (200, "/debug/overview")])
        self.assertTrue(all(r["ip"] for r in rows))

    def test_the_query_is_recorded_but_never_a_token(self):
        dbg("/debug/conversation?user=Tester&limit=5")
        req(f"/debug/overview?token={DEBUG_TOKEN}")
        paths = [r["path"] for r in debug_api.recent_access(5)]
        self.assertIn("/debug/conversation?user=Tester&limit=5", paths)
        self.assertTrue(all(DEBUG_TOKEN not in p for p in paths), "a refused request records its path, not its query string")
        logs = " ".join(r["text"] for r in logbuf.recent())
        self.assertNotIn(DEBUG_TOKEN, logs)

    def test_a_request_while_off_is_not_recorded_because_nothing_answered(self):
        turn_off()
        dbg("/debug/overview")
        self.assertEqual(debug_api.recent_access(5), [])

    def test_the_record_is_capped(self):
        for i in range(debug_api.AUDIT_KEEP + 30):
            debug_api.audit("1.2.3.4", f"/debug/x{i}", 200)
        n = store.read(lambda c: c.execute("SELECT COUNT(*) AS n FROM debug_access_log").fetchone()["n"])
        self.assertLessEqual(n, debug_api.AUDIT_KEEP + 1)

    def test_the_tab_lists_recent_requests(self):
        dbg("/debug/overview")
        page = admin("/settings?tab=debug")[1]
        self.assertIn("/debug/overview", page)


if __name__ == "__main__":
    unittest.main()

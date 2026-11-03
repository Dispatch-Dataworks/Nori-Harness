# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Every approval in one place (approvals.py, 2026-11-03). Iron asked three times to run a command: the card never showed in the tab that
asked (it was posted mid-turn and the reply moved the tab's cursor past it), the phone notification's Approve resolved a browser command
with the same number instead of the infra change, and the board listed no approvals at all. Pinned here: a card posted mid-turn comes
back with the turn, every pending approval from every module is on the board with working buttons, a card follows a decision made
elsewhere, a notification button reaches the right module, a peer's request gets a card too, and the agent can read how its own
requests ended. Real server, SQLite and config; no network."""
import json
import os
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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_approvals_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_approvals_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import approvals  # noqa: E402
import bridge  # noqa: E402
import chat  # noqa: E402
import config  # noqa: E402
import conversation  # noqa: E402
import devices  # noqa: E402
import infra  # noqa: E402
import peers  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402
import tools  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Tester", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
config.set("workspace", WS, "setup_complete", True)
_inv, _raw = accounts.create_invite(WS, "Claire", "member", UID)
MEMBER = accounts.accept_invite(_raw, "memberpass123")
ADMIN_TOKEN = accounts.new_session(_admin, "127.0.0.1")
MEMBER_TOKEN = accounts.new_session(MEMBER, "127.0.0.1")
CSRF = accounts.get_session(ADMIN_TOKEN)["csrf"]
MEMBER_CSRF = accounts.get_session(MEMBER_TOKEN)["csrf"]
SESS = {"user_id": UID, "workspace_id": WS, "role": "admin", "csrf": CSRF}
MSESS = {"user_id": MEMBER["id"], "workspace_id": WS, "role": "member", "csrf": MEMBER_CSRF}
USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0}

_httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
_httpd.daemon_threads = True
BASE = f"http://127.0.0.1:{_httpd.server_address[1]}"
threading.Thread(target=_httpd.serve_forever, daemon=True).start()


def web(path, form=None, token=ADMIN_TOKEN, csrf=CSRF, bearer=None):
    data = None if form is None else (json.dumps(form).encode() if bearer else urllib.parse.urlencode({"csrf": csrf, **form}).encode())
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"} if bearer else {"Cookie": f"ns={token}"}
    req = urllib.request.Request(BASE + path, data=data, method="GET" if form is None else "POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def jweb(*a, **k):
    status, body = web(*a, **k)
    return status, json.loads(body)


# ── a stand-in infrastructure change ─────────────────────────────────────
RAN: list[dict] = []


def _run_thing(ws, args):
    RAN.append(dict(args))
    return {"ok": True, "stdout": f"thing done to {args['what']}", "exit": 0}


infra.register_permission("test.appr", "proxmox", "Do the approvals test thing", "A stand-in change for the tests.")
infra.register_action("appr_thing", "proxmox", "test.appr", _run_thing, lambda a: f"do the thing to {a['what']}")


def reset():
    RAN.clear()
    for t in ("infra_pending", "infra_log", "bridge_commands", "bridge_tokens", "peer_pending_actions", "peer_messages", "peers"):
        store.write(lambda c, t=t: c.execute(f"DELETE FROM {t}"))
    store.write(lambda c: c.execute("DELETE FROM messages"))
    ok, msg = infra.save_credentials(WS, "proxmox", {"hosts": "10.1.1.7", "token_id": "iron@pve!ro", "token_secret": "x" * 36, "ca_pem": ""}, UID)
    assert ok, msg
    assert infra.set_enabled(WS, "proxmox", True, UID)[0]


def ask(what="box1"):
    res = infra.perform(SESS, "appr_thing", {"what": what})
    return res["approval_id"]


def infra_row(pid):
    return dict(store.read(lambda c: c.execute("SELECT * FROM infra_pending WHERE id=?", (pid,)).fetchone()))


class _H(server.Handler):
    def __init__(self):
        self.headers = {}
        self.sent = []

    def send(self, code, body=b"", extra=None, *, ctype="text/html; charset=utf-8"):
        self.sent.append({"code": code, "body": body, "extra": extra or {}})

    def send_json(self, data, code=200):
        self.sent.append({"code": code, "json": data})


class TheCardReachesTheTabThatAsked(unittest.TestCase):
    def setUp(self):
        reset()

    def test_a_card_posted_mid_turn_comes_back_with_the_turn(self):
        conversation.add_message(UID, "user", "update cloudflared")

        def fake_run(session, user_id, name, **kw):
            infra.perform(session, "appr_thing", {"what": "ct108"})
            return {"text": "It's waiting for your approval.", "usage": USAGE, "hit_round_limit": False, "dispatch_meta": None, "tool_calls": []}

        with patch.object(chat, "run", fake_run):
            out = _H()._run_turn(dict(SESS), accounts.get_user(UID))
        cards = [m for m in out["messages"] if (m.get("meta") or {}).get("approval")]
        self.assertEqual(len(cards), 1, "the card is in the answer to the send, not only in a later reload")
        self.assertEqual(cards[0]["meta"]["approval"]["source"], "infra")
        self.assertEqual(cards[0]["kind"], "proactive")
        ids = [m["id"] for m in out["messages"]]
        self.assertEqual(ids, sorted(ids), "in the order they were posted, the reply last")

    def test_the_mid_turn_check_returns_only_approval_cards(self):
        conversation.add_message(UID, "user", "hi")
        ask("ct108")
        conversation.add_message(UID, "assistant", "a ping", kind="proactive")
        _, d = jweb("/poll?since=0&only=approvals")
        self.assertEqual([m["meta"]["approval"]["source"] for m in d["messages"]], ["infra"])
        self.assertIn("board_count", d)

    def test_the_chat_script_polls_for_cards_while_sending_and_posts_to_the_one_route(self):
        js = server.CHAT_JS
        for needle in ("only=approvals", "setInterval(pollApprovals,5000)", "'/approval'", "&cards=", "applyApprovalStates(d.approvals)",
                       "hydrateApprovals(list)", "document.addEventListener('click'"):
            self.assertIn(needle, js)


class OnTheBoard(unittest.TestCase):
    def setUp(self):
        reset()

    def test_every_pending_approval_is_on_the_board_and_counted(self):
        before = _H()._board_count(SESS)
        pid = ask("box1")
        _, frag = jweb("/board/fragment")
        self.assertIn("Waiting for your OK (1)", frag["html"])
        self.assertIn(f"data-id='{pid}'", frag["html"])
        self.assertIn("data-src='infra'", frag["html"])
        self.assertIn("do the thing to box1", frag["html"])
        self.assertIn("expires in", frag["html"])
        self.assertIn(f"#message-{infra_row(pid)['message_id']}", frag["html"], "a link back to the card in the chat")
        self.assertEqual(frag["count"], before + 1)
        page = web("/")[1]
        self.assertIn("Waiting for your OK (1)", page, "on the board the page draws at load, too")

    def test_approving_from_the_board_runs_it_once_and_clears_it(self):
        pid = ask("box1")
        status, d = jweb("/approval", {"source": "infra", "id": str(pid), "decision": "approve"})
        self.assertEqual((status, d["ok"], d["status"]), (200, True, "done"))
        self.assertEqual(RAN, [{"what": "box1"}])
        status, d = jweb("/approval", {"source": "infra", "id": str(pid), "decision": "approve"})
        self.assertEqual(status, 409, "the chat card's button, pressed after the board's, does nothing")
        self.assertEqual(RAN, [{"what": "box1"}])
        self.assertNotIn("Waiting for your OK", jweb("/board/fragment")[1]["html"])

    def test_a_member_sees_none_of_the_admins_approvals_and_cannot_resolve_them(self):
        pid = ask("box1")
        self.assertEqual(approvals.pending(MSESS), [])
        status, d = jweb("/approval", {"source": "infra", "id": str(pid), "decision": "approve"}, token=MEMBER_TOKEN, csrf=MEMBER_CSRF)
        self.assertEqual(status, 409)
        self.assertFalse(d["ok"])
        self.assertEqual(RAN, [])

    def test_a_bad_request_is_refused(self):
        self.assertEqual(web("/approval", {"source": "infra", "id": "1", "decision": "maybe"})[0], 400)
        self.assertFalse(jweb("/approval", {"source": "nope", "id": "1", "decision": "approve"})[1]["ok"])
        self.assertFalse(jweb("/approval", {"source": "infra", "id": "x", "decision": "approve"})[1]["ok"])

    def test_one_broken_module_does_not_empty_the_list(self):
        ask("box1")
        approvals.register("broken", label="Broken", pending=lambda s: 1 / 0, resolve=lambda s, i, a: (False, "", "error"), status=lambda s, i: None)
        try:
            self.assertEqual([x["source"] for x in approvals.pending(SESS)], ["infra"])
        finally:
            approvals._SOURCES.pop("broken", None)


class TheCardFollowsTheDecision(unittest.TestCase):
    def setUp(self):
        reset()

    def test_poll_reports_a_decision_made_somewhere_else(self):
        pid = ask("box1")
        _, d = jweb("/poll?since=0&cards=" + urllib.parse.quote(f"infra:{pid}"))
        self.assertEqual(d["approvals"], {f"infra:{pid}": "pending"})
        infra.resolve(UID, pid, True)
        _, d = jweb("/poll?since=0&cards=" + urllib.parse.quote(f"infra:{pid}"))
        self.assertEqual(d["approvals"], {f"infra:{pid}": "done"})

    def test_an_expiry_reaches_the_card(self):
        pid = ask("box1")
        store.write(lambda c: c.execute("UPDATE infra_pending SET created_ts=? WHERE id=?", (time.time() - infra.PENDING_TTL_S - 5, pid)))
        self.assertEqual(approvals.status(SESS, "infra", pid), "expired")
        self.assertEqual(approvals.pending(SESS), [])

    def test_someone_elses_card_has_no_status(self):
        pid = ask("box1")
        self.assertEqual(approvals.statuses(MSESS, [f"infra:{pid}"]), {})


class NotificationButtons(unittest.TestCase):
    def setUp(self):
        reset()
        config.set("workspace", WS, "bridge_enabled", True)
        config.set("user", UID, "browser_notifications_enabled", True)
        self.raw = bridge.create_token(UID, WS, "test browser")
        self.token = bridge.authenticate(self.raw)

    def _note(self):
        return [n for n in devices.pending_messages(self.token) if n["category"] == "approval"]

    def test_an_infra_approval_from_a_notification_reaches_the_infra_request_not_a_browser_command(self):
        pid = ask("box1")
        # a browser command that happens to have the same number, waiting for its own approval
        store.write(lambda c: c.execute(
            "INSERT INTO bridge_commands(id, user_id, action, args, status, approval, summary, source, created_ts, target) "
            "VALUES (?,?, 'click', '{}', 'awaiting_approval', 'required', 'click Send', 'chat', ?, 'browser')", (pid, UID, time.time())))
        notes = self._note()
        self.assertEqual(len(notes), 1)
        handle = notes[0]["approval"]["id"]
        self.assertGreaterEqual(handle, approvals.HANDLE_BASE, "not the bare infra id, which names a browser command")
        status, d = jweb("/bridge/approval", {"id": handle, "decision": "approve"}, bearer=self.raw)
        self.assertEqual((status, d["ok"], d["status"]), (200, True, "done"), d)
        self.assertEqual(RAN, [{"what": "box1"}])
        self.assertEqual(bridge._cmd(pid)["status"], "awaiting_approval", "the browser command is untouched")

    def test_a_browser_command_keeps_its_own_number(self):
        self.assertEqual(approvals.notify_handle("bridge", 12, 900), 12)
        self.assertEqual(approvals.notify_handle(None, 12, 900), 12)
        self.assertEqual(approvals.from_handle(UID, 12), ("bridge", 12))

    def test_a_handle_for_someone_elses_message_resolves_nothing(self):
        ask("box1")
        mid = infra_row(store.read(lambda c: c.execute("SELECT MAX(id) AS m FROM infra_pending").fetchone())["m"])["message_id"]
        self.assertIsNone(approvals.from_handle(MEMBER["id"], approvals.HANDLE_BASE + mid))
        self.assertIsNone(approvals.from_handle(UID, approvals.HANDLE_BASE + 999999))


class PeerRequests(unittest.TestCase):
    def setUp(self):
        reset()
        self.peer_id = store.write(lambda c: c.execute(
            "INSERT INTO peers(name, url, scope, scope_id, self_agent_id, psk_enc, enabled, trust_level, expiry_hours, created_ts, created_by) "
            "VALUES ('Sage', 'https://sage.invalid/paci/1', 'user', ?, 'iron', 'x', 1, 'prompt', 24, ?, ?)", (UID, time.time(), UID)).lastrowid)
        tools.register(tools.Tool("appr_peer_thing", {"type": "function", "function": {"name": "appr_peer_thing", "description": "test",
                                                                                         "parameters": {"type": "object", "properties": {}}}},
                                  lambda session, **a: {"ok": True, "note": "peer thing done"}, min_role="member", data_scope="self", risk_tier="B"))
        peers._PEER_REQUESTABLE.add("appr_peer_thing")
        store.write(lambda c: c.execute(
            "INSERT INTO peer_messages(peer_id, conversation_id, direction, message_id, type, seq, body_json, ts) "
            "VALUES (?, 'c-1', 'received', 'm-1', 'message', 1, ?, ?)",
            (self.peer_id, json.dumps({"text": "please", "suspicious": False}), time.time())))

    def tearDown(self):
        peers._PEER_REQUESTABLE.discard("appr_peer_thing")
        tools.unregister("appr_peer_thing")

    def _ask(self):
        return peers._make_act_impl(self.peer_id)(dict(SESS), action="appr_peer_thing", source_message_id="m-1", args={"room": "kitchen"})

    def test_a_held_peer_request_gets_a_card_and_a_place_on_the_board(self):
        self.assertTrue(self._ask()["pending"])
        row = store.read(lambda c: c.execute("SELECT * FROM peer_pending_actions ORDER BY id DESC LIMIT 1").fetchone())
        msg = store.read(lambda c: c.execute("SELECT meta FROM messages WHERE id=?", (row["message_id"],)).fetchone())
        card = json.loads(msg["meta"])["approval"]
        self.assertEqual((card["source"], card["id"], card["status"]), ("peer", row["id"], "pending"))
        self.assertIn('"room": "kitchen"', card["summary"], "what it would run, shown before it is approved")
        items = [x for x in approvals.pending(SESS) if x["source"] == "peer"]
        self.assertEqual(len(items), 1)
        self.assertIn("Sage asks me to run appr_peer_thing", items[0]["summary"])

    def test_approving_it_runs_it_once_and_closes_the_card(self):
        self._ask()
        aid = approvals.pending(SESS)[0]["id"]
        ok, msg, status = approvals.resolve(SESS, "peer", aid, True)
        self.assertEqual((ok, status), (True, "done"), msg)
        self.assertFalse(approvals.resolve(SESS, "peer", aid, True)[0], "a second click does not run it again")
        row = store.read(lambda c: c.execute("SELECT * FROM peer_pending_actions WHERE id=?", (aid,)).fetchone())
        meta = json.loads(store.read(lambda c: c.execute("SELECT meta FROM messages WHERE id=?", (row["message_id"],)).fetchone())["meta"])
        self.assertEqual(meta["approval"]["status"], "done")
        self.assertEqual(approvals.pending(SESS), [])


class TheAgentCanReadHowItsRequestsEnded(unittest.TestCase):
    def setUp(self):
        reset()

    def test_infra_requests_shows_the_output_of_an_approved_change(self):
        pid = ask("ct108")
        out = tools.dispatch("infra_requests", {}, SESS)
        self.assertEqual(out["requests"][0]["status"], "pending")
        self.assertIn("Do not ask again", out["requests"][0]["note"])
        infra.resolve(UID, pid, True)
        out = tools.dispatch("infra_requests", {"request_id": pid}, SESS)
        self.assertEqual(out["requests"][0]["status"], "done")
        self.assertEqual(out["requests"][0]["result"]["stdout"], "thing done to ct108")
        self.assertIn("not instructions", out["note"])

    def test_only_your_own_requests(self):
        ask("ct108")
        self.assertNotIn("infra_requests", {s["function"]["name"] for s in tools.active_schemas(MSESS)})
        other = {**SESS, "user_id": MEMBER["id"]}
        self.assertEqual(tools.dispatch("infra_requests", {}, other).get("requests", []), [])


if __name__ == "__main__":
    unittest.main()

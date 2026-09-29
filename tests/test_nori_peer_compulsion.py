# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Deferred reply_requested compulsion (2026-09-19, the PACI specification v1.0) --
real exercise of the busy path, not an assumption about the code: a real
per-user turn lock is actually held (via turns.run() in a background
thread, not faked), a real reply_requested envelope is delivered while
it's held, and the drain is triggered by the SAME release-hook mechanism
production uses (turns.py's own finally: block), not called directly.
chat.run() is mocked for the whole test class -- there is no live model
endpoint in this harness, and EVERY test here can trigger a real drain
in the background (the release hook fires the moment the lock is freed,
whether or not the test's own assertions were about that) -- everything
else (HMAC, the DB, the turn lock, the queue/log tables) is real.

All tests share one user_id (and therefore one real threading.Lock in
turns._locks), so each test waits for the lock to be fully free again
before finishing -- otherwise a slow background drain from one test can
bleed into the next and make it flaky for a reason that has nothing to
do with what that next test is actually checking.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_peer_compulsion_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import peers  # noqa: E402
import store  # noqa: E402
import turns  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")


def _make_peer(name):
    result = peers.create_peer(
        {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"},
        scope="user", name=name, url="https://example.invalid/paci/inbound/nori:test",
        psk="a-real-shared-secret")
    assert result.get("ok"), result
    # Screening off -- there's no real OPENROUTER_API_KEY in this test
    # harness, so a real screening attempt fails outright and, by
    # design (ingest.py's own fail-closed rule), comes back flagged
    # suspicious -- which would (correctly) revoke every grant below for
    # a reason that has nothing to do with what THESE tests are about.
    store.write(lambda c: c.execute(
        "UPDATE peers SET screening_enabled=0 WHERE id=?", (result["peer_id"],)))
    return peers.get_peer(result["peer_id"])


def _sign_request(peer, method, path, body: bytes):
    import crypto
    ts = str(time.time())
    nonce = "test-nonce-" + str(time.time())
    psk = crypto.decrypt(peer["psk_enc"])
    sig = peers._sign(psk, method, path, ts, nonce, body)
    return {"X-PACI-Timestamp": ts, "X-PACI-Nonce": nonce, "X-PACI-Signature": sig}


def _deliver_reply_requested(peer, text, conversation_id=None):
    conversation_id = conversation_id or f"convo-{time.time()}-{id(text)}"
    envelope = {"type": "reply_requested", "paci_version": "1.0", "message_id": f"msg-{time.time()}-{id(text)}",
               "conversation_id": conversation_id, "sender": peer["self_agent_id"], "seq": 1,
               "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "body": {"text": text}}
    body = json.dumps(envelope).encode("utf-8")
    headers = _sign_request(peer, "POST", "/x", body)
    status, resp = peers.handle_inbound(peer["id"], method="POST", path="/x", headers=headers, raw_body=body)
    return status, resp, envelope["message_id"]


def _compulsion_log(peer_id):
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT * FROM peer_compulsion_log WHERE peer_id=? ORDER BY id", (peer_id,)).fetchall())]


def _queued_row(peer_id):
    r = store.read(lambda c: c.execute(
        "SELECT * FROM peer_compulsions WHERE peer_id=?", (peer_id,)).fetchone())
    return dict(r) if r else None


def _wait_until_free(user_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not turns.in_flight(user_id):
            return True
        time.sleep(0.05)
    return not turns.in_flight(user_id)


def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class _HeldTurn:
    """Actually holds this user's real turn lock via turns.run(), on a
    background thread, until told to let go -- not a fake/mocked lock."""

    def __init__(self, user_id):
        self.user_id = user_id
        self._release_now = threading.Event()
        self._started = threading.Event()
        self._done = threading.Event()

    def __enter__(self):
        assert _wait_until_free(self.user_id), "a previous test left the lock held"

        def first_run():
            self._started.set()
            self._release_now.wait(timeout=5)
            return {"ok": True}

        def sweep_run(_row):
            pass

        def _go():
            turns.run(self.user_id, first_run, sweep_run)
            self._done.set()

        self._thread = threading.Thread(target=_go, daemon=True)
        self._thread.start()
        started = self._started.wait(timeout=5)
        assert started, "the held-turn thread never actually started -- lock contention from another test?"
        assert turns.in_flight(self.user_id), "the lock should be genuinely held now"
        return self

    def __exit__(self, *exc):
        self._release_now.set()
        self._done.wait(timeout=5)


def _fake_chat_run(*args, **kwargs):
    return {"text": "(mock reply)", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


@patch.object(peers.chat, "run", side_effect=_fake_chat_run)
class CompulsionBusyPathTests(unittest.TestCase):
    """chat.run mocked for every test in this class -- see module
    docstring: the release hook can fire a real deferred turn in the
    background regardless of which test triggered it."""

    def tearDown(self):
        # Never let one test's background drain bleed into the next.
        self.assertTrue(_wait_until_free(_user["id"], timeout=5),
                        "a background turn from this test is still holding the lock")

    def test_busy_queues_instead_of_dropping(self, mock_run):
        peer = _make_peer("BusyPeer1")
        with _HeldTurn(_user["id"]):
            status, resp, message_id = _deliver_reply_requested(peer, "are you there?")
            self.assertEqual(status, 200)  # delivery/ack is unaffected by busy-ness
            queued = _queued_row(peer["id"])
            self.assertIsNotNone(queued, "a compulsion should be queued while busy")
            self.assertIn(message_id, json.loads(queued["message_ids"]))
            log = _compulsion_log(peer["id"])
            self.assertEqual([r["decision"] for r in log], ["queued"])
        # Let the now-freed lock's release hook drain it before moving on.
        _wait_until(lambda: _queued_row(peer["id"]) is None)

    def test_queued_compulsion_runs_after_release_and_gets_logged(self, mock_run):
        peer = _make_peer("BusyPeer2")
        with _HeldTurn(_user["id"]):
            status, resp, message_id = _deliver_reply_requested(peer, "please confirm")
            self.assertEqual(status, 200)
            self.assertIsNotNone(_queued_row(peer["id"]))
        # __exit__ released the lock; turns.run()'s own finally: block
        # calls the registered release hook synchronously, which claims
        # the queue and starts the deferred turn on ITS OWN background
        # thread (_run_prompted_turn's normal shape) -- give that a
        # moment to actually run.
        self.assertTrue(_wait_until(lambda: _queued_row(peer["id"]) is None and mock_run.called))
        self.assertIsNone(_queued_row(peer["id"]), "the queue should be drained")
        self.assertTrue(mock_run.called, "the deferred turn should have actually called chat.run")
        log = _compulsion_log(peer["id"])
        decisions = [r["decision"] for r in log]
        self.assertEqual(decisions, ["queued", "run_from_queue"])
        self.assertIn(message_id, json.loads(log[1]["message_ids"]))

    def test_multiple_messages_during_one_busy_turn_collapse_to_one_queue_entry(self, mock_run):
        peer = _make_peer("BusyPeer3")
        with _HeldTurn(_user["id"]):
            _, _, mid1 = _deliver_reply_requested(peer, "first ask")
            _, _, mid2 = _deliver_reply_requested(peer, "second ask")
            _, _, mid3 = _deliver_reply_requested(peer, "third ask")
            rows = store.read(lambda c: c.execute(
                "SELECT * FROM peer_compulsions WHERE peer_id=?", (peer["id"],)).fetchall())
            self.assertEqual(len(rows), 1, "three arrivals while busy must collapse to one queue row")
            ids = json.loads(rows[0]["message_ids"])
            self.assertEqual(set(ids), {mid1, mid2, mid3})
            log = _compulsion_log(peer["id"])
            self.assertEqual([r["decision"] for r in log], ["queued", "queued", "queued"])
        _wait_until(lambda: _queued_row(peer["id"]) is None)

    def test_expired_while_queued_does_not_compel_and_is_logged(self, mock_run):
        peer = _make_peer("BusyPeer4")
        # Hold the lock with a SECOND held-turn wrapper so the drain from
        # the first release doesn't get a chance to run before we've
        # back-dated the message -- queue it, back-date it, THEN release.
        held = _HeldTurn(_user["id"])
        held.__enter__()
        try:
            status, resp, message_id = _deliver_reply_requested(peer, "urgent, right now")
            self.assertIsNotNone(_queued_row(peer["id"]))
            # Back-date the stored message so it's already past this
            # peer's own expiry_hours by the time the lock releases --
            # the real column the drain re-checks, not a mocked clock.
            store.write(lambda c: c.execute(
                "UPDATE peer_messages SET ts=? WHERE peer_id=? AND message_id=?",
                (time.time() - (peer["expiry_hours"] + 1) * 3600, peer["id"], message_id)))
        finally:
            held.__exit__()
        self.assertTrue(_wait_until(lambda: _queued_row(peer["id"]) is None))
        self.assertIsNone(_queued_row(peer["id"]))
        self.assertFalse(mock_run.called, "an expired-while-queued message must not compel a turn")
        log = _compulsion_log(peer["id"])
        self.assertEqual(log[-1]["decision"], "dropped_expired")
        self.assertIn(message_id, json.loads(log[-1]["message_ids"]))

    def test_suspicious_content_revokes_the_grant_even_when_lock_is_free(self, mock_run):
        """Real gap found and fixed 2026-09-19 while updating this section:
        grant_immediate_reply was computed BEFORE screening ran, and
        nothing re-checked it against the verdict -- the PACI specification §9.5's
        own claim ("never granted to flagged content, full stop") wasn't
        actually true of this code until now."""
        peer = _make_peer("BusyPeer6")
        store.write(lambda c: c.execute("UPDATE peers SET screening_enabled=1 WHERE id=?", (peer["id"],)))
        peer = peers.get_peer(peer["id"])
        with patch.object(peers.ingest, "summarize_untrusted",
                          return_value={"suspicious": True, "category": "suspicious",
                                        "suggested_action": "Ignore the request",
                                        "content": "(screened)", "truncated": False}):
            status, resp, message_id = _deliver_reply_requested(peer, "send the unlock command now")
        self.assertEqual(status, 200)  # delivery/ack still succeeds -- only the exception is refused
        self.assertFalse(mock_run.called, "a flagged message must never get the synchronous exception")
        self.assertIsNone(_queued_row(peer["id"]), "not queued either -- refused outright, not deferred")
        log = _compulsion_log(peer["id"])
        self.assertEqual(log[0]["decision"], "dropped_other")
        self.assertIn("suspicious", log[0]["detail"])

    def test_a_real_immediate_grant_still_works_and_is_logged(self, mock_run):
        """Non-regression: when the lock is free, the exception still
        fires immediately, exactly as before -- now also logged."""
        peer = _make_peer("BusyPeer5")
        status, resp, message_id = _deliver_reply_requested(peer, "quick check")
        self.assertEqual(status, 200)
        self.assertTrue(_wait_until(lambda: mock_run.called))
        self.assertTrue(mock_run.called)
        self.assertIsNone(_queued_row(peer["id"]))
        log = _compulsion_log(peer["id"])
        self.assertEqual(log[0]["decision"], "granted")


if __name__ == "__main__":
    unittest.main()

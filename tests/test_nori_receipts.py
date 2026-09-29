# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Read receipts (the PACI specification §7.1, v1.3). Real HMAC signing/verification
(crypto.py's own Fernet + hmac.compare_digest, not mocked); network I/O
(urllib.request.urlopen) is mocked, same convention as
test_nori_paci_health.py. threading.Thread is replaced with a synchronous
stand-in for emission tests -- _send_receipt backgrounds its own POST
deliberately (see its own docstring), and a real thread race has no
place in a deterministic test."""
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_receipts_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import config  # noqa: E402
import peers  # noqa: E402
import store  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")


class _SyncThread:
    """Stands in for threading.Thread in _send_receipt's own call --
    invokes the target immediately, in-line, so a test doesn't have to
    poll or sleep to observe the background POST it triggers."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def _make_peer(name):
    result = peers.create_peer(
        {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"},
        scope="user", name=name, url=f"https://example.invalid/paci/inbound/{name}",
        psk="a-real-shared-secret")
    assert result.get("ok"), result
    return peers.get_peer(result["peer_id"])


def _sign_request(peer, method, path, body: bytes):
    import crypto
    ts = str(time.time())
    nonce = f"test-nonce-{uuid.uuid4().hex}"
    psk = crypto.decrypt(peer["psk_enc"])
    sig = peers._sign(psk, method, path, ts, nonce, body)
    return {"X-PACI-Timestamp": ts, "X-PACI-Nonce": nonce, "X-PACI-Signature": sig}


def _insert_received(peer_id, text, *, message_id=None, ts=None, presented_peer=False, presented_user=False):
    ts = time.time() if ts is None else ts
    message_id = message_id or str(uuid.uuid4())
    store.write(lambda c: c.execute(
        "INSERT INTO peer_messages(peer_id, conversation_id, message_id, direction, type, seq, "
        "body_json, ts, status, presented_peer_ts, presented_user_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (peer_id, str(uuid.uuid4()), message_id, "received", "message", 1,
         json.dumps({"content": text, "suspicious": False}), ts, "delivered",
         ts if presented_peer else None, ts if presented_user else None)))
    return message_id


def _insert_sent(peer_id, text, *, message_id=None, ts=None):
    ts = time.time() if ts is None else ts
    message_id = message_id or str(uuid.uuid4())
    store.write(lambda c: c.execute(
        "INSERT INTO peer_messages(peer_id, conversation_id, message_id, direction, type, seq, "
        "body_json, ts, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (peer_id, str(uuid.uuid4()), message_id, "sent", "message", 1,
         json.dumps({"text": text}), ts, "pending")))
    return message_id


class GranularitySettingTests(unittest.TestCase):
    def test_default_is_off(self):
        peer = _make_peer(f"gran-default-{uuid.uuid4().hex[:8]}")
        self.assertEqual(peer["receipt_granularity"], "off")

    def test_set_and_read_back(self):
        peer = _make_peer(f"gran-set-{uuid.uuid4().hex[:8]}")
        session = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"}
        result = peers.set_receipt_granularity(session, peer["id"], "full")
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(peers.get_peer(peer["id"])["receipt_granularity"], "full")

    def test_invalid_value_rejected(self):
        peer = _make_peer(f"gran-bad-{uuid.uuid4().hex[:8]}")
        session = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"}
        result = peers.set_receipt_granularity(session, peer["id"], "everything")
        self.assertIn("error", result)
        self.assertEqual(peers.get_peer(peer["id"])["receipt_granularity"], "off")  # unchanged


class InboundReceiptTests(unittest.TestCase):
    def test_valid_receipt_stored_never_a_turn(self):
        peer = _make_peer(f"inbound-ok-{uuid.uuid4().hex[:8]}")
        message_id = str(uuid.uuid4())
        envelope = {"type": "receipt", "paci_version": "1.0", "message_id": message_id,
                   "stage": "surfaced", "at": "2026-09-19T14:02:00.000Z", "context": "user"}
        body = json.dumps(envelope).encode("utf-8")
        headers = _sign_request(peer, "POST", "/x", body)
        status, resp = peers.handle_inbound(peer["id"], method="POST", path="/x", headers=headers, raw_body=body)
        self.assertEqual(status, 200)
        self.assertEqual(resp["type"], "receipt_ack")
        row = store.read(lambda c: c.execute(
            "SELECT * FROM peer_receipts WHERE peer_id=? AND message_id=?",
            (peer["id"], message_id)).fetchone())
        self.assertIsNotNone(row)
        self.assertEqual(row["stage"], "surfaced")
        self.assertEqual(row["context"], "user")
        # Never a turn: no conversation touched.
        convo_count = store.read(lambda c: c.execute(
            "SELECT count(*) AS n FROM peer_conversations WHERE peer_id=?", (peer["id"],)).fetchone())["n"]
        self.assertEqual(convo_count, 0)

    def test_duplicate_receipt_idempotent(self):
        peer = _make_peer(f"inbound-dup-{uuid.uuid4().hex[:8]}")
        message_id = str(uuid.uuid4())
        envelope = {"type": "receipt", "paci_version": "1.0", "message_id": message_id,
                   "stage": "presented", "at": "2026-09-19T14:02:00.000Z", "context": "peer"}
        for _ in range(2):
            body = json.dumps(envelope).encode("utf-8")
            headers = _sign_request(peer, "POST", "/x", body)
            status, _resp = peers.handle_inbound(peer["id"], method="POST", path="/x", headers=headers, raw_body=body)
            self.assertEqual(status, 200)
        count = store.read(lambda c: c.execute(
            "SELECT count(*) AS n FROM peer_receipts WHERE peer_id=? AND message_id=?",
            (peer["id"], message_id)).fetchone())["n"]
        self.assertEqual(count, 1)  # not two rows

    def test_malformed_receipt_rejected(self):
        peer = _make_peer(f"inbound-bad-{uuid.uuid4().hex[:8]}")
        envelope = {"type": "receipt", "paci_version": "1.0", "stage": "nonsense", "context": "peer"}
        body = json.dumps(envelope).encode("utf-8")
        headers = _sign_request(peer, "POST", "/x", body)
        status, resp = peers.handle_inbound(peer["id"], method="POST", path="/x", headers=headers, raw_body=body)
        self.assertEqual(status, 400)
        self.assertEqual(resp["type"], "error")

    def test_bad_signature_rejected_before_type_dispatch(self):
        peer = _make_peer(f"inbound-sig-{uuid.uuid4().hex[:8]}")
        envelope = {"type": "receipt", "paci_version": "1.0", "message_id": str(uuid.uuid4()),
                   "stage": "presented", "at": "2026-09-19T14:02:00.000Z", "context": "peer"}
        body = json.dumps(envelope).encode("utf-8")
        headers = _sign_request(peer, "POST", "/x", body)
        headers["X-PACI-Signature"] = "0" * 64
        status, resp = peers.handle_inbound(peer["id"], method="POST", path="/x", headers=headers, raw_body=body)
        self.assertEqual(status, 401)


class EmissionGatingTests(unittest.TestCase):
    """_maybe_send_receipt's own gate, exercised with a real (mocked
    transport) POST attempt -- not just reading the source to confirm
    the branches exist."""

    def _urlopen_ok(self):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"type": "receipt_ack"}).encode("utf-8")
        resp.__enter__.return_value = resp
        return resp

    def test_off_sends_nothing(self):
        peer = _make_peer(f"emit-off-{uuid.uuid4().hex[:8]}")
        self.assertEqual(peer["receipt_granularity"], "off")
        with patch("peers.threading.Thread", _SyncThread), \
             patch("urllib.request.urlopen") as mock_urlopen:
            peers._maybe_send_receipt(peer, "msg-1", "presented", "peer")
        mock_urlopen.assert_not_called()

    def test_coarse_sends_presented_not_surfaced(self):
        peer = _make_peer(f"emit-coarse-{uuid.uuid4().hex[:8]}")
        session = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"}
        peers.set_receipt_granularity(session, peer["id"], "coarse")
        peer = peers.get_peer(peer["id"])
        with patch("peers.threading.Thread", _SyncThread), \
             patch("urllib.request.urlopen", return_value=self._urlopen_ok()) as mock_urlopen:
            peers._maybe_send_receipt(peer, "msg-1", "presented", "peer")
        mock_urlopen.assert_called_once()
        sent_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(sent_body["stage"], "presented")
        self.assertEqual(sent_body["context"], "peer")
        self.assertEqual(sent_body["message_id"], "msg-1")
        self.assertEqual(sent_body["type"], "receipt")
        with patch("peers.threading.Thread", _SyncThread), \
             patch("urllib.request.urlopen", return_value=self._urlopen_ok()) as mock_urlopen2:
            peers._maybe_send_receipt(peer, "msg-1", "surfaced", "user")
        mock_urlopen2.assert_not_called()

    def test_full_sends_both_stages(self):
        peer = _make_peer(f"emit-full-{uuid.uuid4().hex[:8]}")
        session = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"}
        peers.set_receipt_granularity(session, peer["id"], "full")
        peer = peers.get_peer(peer["id"])
        for stage, context in (("presented", "peer"), ("surfaced", "user")):
            with patch("peers.threading.Thread", _SyncThread), \
                 patch("urllib.request.urlopen", return_value=self._urlopen_ok()) as mock_urlopen:
                peers._maybe_send_receipt(peer, "msg-1", stage, context)
            mock_urlopen.assert_called_once()

    def test_send_failure_never_raises(self):
        peer = _make_peer(f"emit-fail-{uuid.uuid4().hex[:8]}")
        session = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"}
        peers.set_receipt_granularity(session, peer["id"], "full")
        peer = peers.get_peer(peer["id"])
        import urllib.error
        with patch("peers.threading.Thread", _SyncThread), \
             patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            peers._maybe_send_receipt(peer, "msg-1", "presented", "peer")  # must not raise


class EndToEndEmissionTests(unittest.TestCase):
    """The real hook inside pending_delivery_messages() -- not calling
    _maybe_send_receipt directly, but exercising the actual code path
    that fires it after a real presented_*_ts stamp."""

    def test_presenting_a_message_emits_a_receipt(self):
        peer = _make_peer(f"e2e-{uuid.uuid4().hex[:8]}")
        session = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"}
        peers.set_receipt_granularity(session, peer["id"], "full")
        message_id = _insert_received(peer["id"], "hello from the other side")
        resp = MagicMock()
        resp.read.return_value = json.dumps({"type": "receipt_ack"}).encode("utf-8")
        resp.__enter__.return_value = resp
        with patch("peers.threading.Thread", _SyncThread), \
             patch("urllib.request.urlopen", return_value=resp) as mock_urlopen:
            peers.pending_delivery_messages(_user["id"], dimension="peer")
        mock_urlopen.assert_called_once()
        sent_body = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(sent_body["stage"], "presented")
        self.assertEqual(sent_body["context"], "peer")
        self.assertEqual(sent_body["message_id"], message_id)


class ReceiptDisplayTests(unittest.TestCase):
    """recent_context_block()'s own receipt annotation on a sent row."""

    def setUp(self):
        self.peer = _make_peer(f"display-{uuid.uuid4().hex[:8]}")
        config.set("workspace", _user["workspace_id"], "peer_recent_cap", 5)
        config.set("workspace", _user["workspace_id"], "peer_recent_window_hours", 12)

    def test_receipt_annotation_appears_on_sent_message(self):
        message_id = _insert_sent(self.peer["id"], "checking in on the household")
        store.write(lambda c: c.execute(
            "INSERT INTO peer_receipts(peer_id, message_id, stage, context, at, received_ts) "
            "VALUES (?,?,?,?,?,?)",
            (self.peer["id"], message_id, "surfaced", "user", "2026-09-19T14:02:00.000Z", time.time())))
        block = peers.recent_context_block(_user["id"])
        self.assertIn("checking in on the household", block)
        self.assertIn("read by them", block)
        self.assertIn("talking to their own operator", block)

    def test_no_receipt_no_annotation(self):
        # Scoped to THIS test's own line, not the whole block -- other
        # tests in this class share the same real DB and may have their
        # own, legitimately-receipted messages from a different peer
        # showing up in the same recent-exchanges block.
        _insert_sent(self.peer["id"], "a message nobody's confirmed reading yet")
        block = peers.recent_context_block(_user["id"])
        line = next(ln for ln in block.splitlines() if "a message nobody's confirmed reading yet" in ln)
        self.assertNotIn("read by them", line)

    def test_both_stages_shown_when_both_received(self):
        message_id = _insert_sent(self.peer["id"], "a fuller update")
        now = time.time()
        store.write(lambda c: c.execute(
            "INSERT INTO peer_receipts(peer_id, message_id, stage, context, at, received_ts) "
            "VALUES (?,?,?,?,?,?)",
            (self.peer["id"], message_id, "presented", "peer", "2026-09-19T14:00:00.000Z", now - 120)))
        store.write(lambda c: c.execute(
            "INSERT INTO peer_receipts(peer_id, message_id, stage, context, at, received_ts) "
            "VALUES (?,?,?,?,?,?)",
            (self.peer["id"], message_id, "surfaced", "user", "2026-09-19T14:02:00.000Z", now - 30)))
        block = peers.recent_context_block(_user["id"])
        self.assertIn("in their own peer channel", block)
        self.assertIn("talking to their own operator", block)


if __name__ == "__main__":
    unittest.main()

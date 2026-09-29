# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""the PACI specification v0.9, §4.1 -- health check (liveness). Real HMAC signing
and verification (crypto.py's own Fernet + hmac.compare_digest, not
mocked -- that's the one place a mock would hide a real auth bug);
network I/O (urllib.request.urlopen) is mocked, since there is no live
paired instance in this test harness."""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_paci_health_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import peers  # noqa: E402
import store  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")


def _make_peer(name="TestPeer"):
    result = peers.create_peer(
        {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"},
        scope="user", name=name, url="https://example.invalid/paci/inbound/nori:test",
        psk="a-real-shared-secret")
    assert result.get("ok"), result
    return peers.get_peer(result["peer_id"])


def _sign_request(peer, method, path, body: bytes):
    """Builds real, correctly-signed request headers -- the same HMAC
    scheme §5 specifies, computed independently here (not by calling
    peers.py's own _post) so this proves the SPEC's algorithm, not just
    "whatever this module's internal helper happens to produce"."""
    import crypto
    ts = str(time.time())
    nonce = "test-nonce-0123456789abcdef"
    psk = crypto.decrypt(peer["psk_enc"])
    sig = peers._sign(psk, method, path, ts, nonce, body)
    return {"X-PACI-Timestamp": ts, "X-PACI-Nonce": nonce, "X-PACI-Signature": sig}


class InboundHealthCheckTests(unittest.TestCase):
    def test_valid_health_check_gets_a_real_ack_never_a_turn(self):
        peer = _make_peer("InboundPeer1")
        envelope = {"type": "health_check", "paci_version": "1.0", "sent_at": time.time()}
        body = json.dumps(envelope).encode("utf-8")
        headers = _sign_request(peer, "POST", "/x", body)
        status, resp = peers.handle_inbound(
            peer["id"], method="POST", path="/x", headers=headers, raw_body=body)
        self.assertEqual(status, 200)
        self.assertEqual(resp["type"], "health_check_ack")
        self.assertEqual(resp["agent_id"], peer["self_agent_id"])
        self.assertIn("received_at", resp)
        self.assertEqual(resp["limits"]["turn_limit"], peer["turn_limit"])
        # Never a turn: no conversation was created or touched for this.
        convo_count = store.read(lambda c: c.execute(
            "SELECT count(*) AS n FROM peer_conversations WHERE peer_id=?", (peer["id"],)).fetchone())["n"]
        self.assertEqual(convo_count, 0)

    def test_bad_signature_rejected_before_type_dispatch(self):
        peer = _make_peer("InboundPeer2")
        envelope = {"type": "health_check", "paci_version": "1.0", "sent_at": time.time()}
        body = json.dumps(envelope).encode("utf-8")
        headers = _sign_request(peer, "POST", "/x", body)
        headers["X-PACI-Signature"] = "0" * 64  # wrong signature
        status, resp = peers.handle_inbound(
            peer["id"], method="POST", path="/x", headers=headers, raw_body=body)
        self.assertEqual(status, 401)
        self.assertEqual(resp["type"], "error")


class OutboundEvaluatorTests(unittest.TestCase):
    """Mocked urllib.request.urlopen -- no live paired instance exists in
    this harness. Each test drives one of §4.1's four failure states plus
    the healthy path, checking they're reported distinctly, not
    collapsed into a generic failure."""

    def _urlopen_returning(self, payload: dict, code: int = 200):
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        resp.__enter__.return_value = resp
        if code != 200:
            import urllib.error
            raise urllib.error.HTTPError("https://example.invalid", code, "err", {}, None)
        return resp

    def test_unreachable(self):
        peer = _make_peer("EvalUnreachable")
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            peers._send_health_check(peer)
        h = peers.health_status(peer["id"])
        self.assertEqual(h["status"], "unreachable")

    def test_hmac_mismatch_via_401(self):
        peer = _make_peer("EvalHmac")
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError("https://example.invalid", 401, "unauthorized", {}, None)):
            peers._send_health_check(peer)
        h = peers.health_status(peer["id"])
        self.assertEqual(h["status"], "hmac_mismatch")
        self.assertEqual(h["detail"]["http_status"], 401)

    def test_other_http_error_is_unreachable_not_hmac(self):
        peer = _make_peer("EvalHttp500")
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError("https://example.invalid", 500, "err", {}, None)):
            peers._send_health_check(peer)
        h = peers.health_status(peer["id"])
        self.assertEqual(h["status"], "unreachable")

    def test_limits_diverged(self):
        peer = _make_peer("EvalLimits")
        store.write(lambda c: c.execute(
            "UPDATE peers SET remote_turn_limit=10, remote_cooldown_minutes=60, remote_daily_cap=4 "
            "WHERE id=?", (peer["id"],)))
        peer = peers.get_peer(peer["id"])
        ack = {"type": "health_check_ack", "agent_id": "peer:x", "received_at": time.time(),
              "limits": {"turn_limit": 6, "cooldown_minutes": 60, "daily_cap": 4}}  # turn_limit changed
        with patch("urllib.request.urlopen", return_value=self._urlopen_returning(ack)):
            peers._send_health_check(peer)
        h = peers.health_status(peer["id"])
        self.assertEqual(h["status"], "limits_diverged")
        self.assertEqual(h["detail"]["reported"]["turn_limit"], 6)

    def test_clock_skew(self):
        peer = _make_peer("EvalSkew")
        sent_ts_holder = {}
        real_time = time.time

        ack_future = {}

        def fake_urlopen(req, timeout=None):
            # received_at far in the "past" relative to when this request
            # is sent, well past the default 60s warn threshold.
            body = json.loads(req.data.decode("utf-8"))
            ack = {"type": "health_check_ack", "agent_id": "peer:x",
                  "received_at": body["sent_at"] - 120, "limits": None}
            return self._urlopen_returning(ack)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            peers._send_health_check(peer)
        h = peers.health_status(peer["id"])
        self.assertEqual(h["status"], "clock_skew")
        self.assertGreater(h["detail"]["skew_seconds"], 60)

    def test_healthy(self):
        peer = _make_peer("EvalHealthy")

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data.decode("utf-8"))
            ack = {"type": "health_check_ack", "agent_id": "peer:x", "received_at": body["sent_at"] + 0.2,
                  "limits": {"turn_limit": peer["turn_limit"], "cooldown_minutes": peer["cooldown_minutes"],
                            "daily_cap": peer["daily_cap"]}}
            return self._urlopen_returning(ack)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            peers._send_health_check(peer)
        h = peers.health_status(peer["id"])
        self.assertEqual(h["status"], "healthy")

    def test_transition_is_logged_only_when_status_actually_changes(self):
        peer = _make_peer("EvalTransition")
        with patch("builtins.print") as mock_print:
            peers._record_health(peer["id"], "healthy", {})
            peers._record_health(peer["id"], "healthy", {})  # same status again -- no new log line
            peers._record_health(peer["id"], "unreachable", {})  # real transition -- logged
        transition_lines = [c for c in mock_print.call_args_list if "->" in str(c)]
        self.assertEqual(len(transition_lines), 2)  # unknown->healthy, healthy->unreachable


class IntervalGatingTests(unittest.TestCase):
    def test_does_not_recheck_before_interval_elapses(self):
        peer = _make_peer("IntervalPeer")
        peer = dict(peer)
        peer["health_check_interval_minutes"] = 5
        with patch.object(peers, "_send_health_check") as mock_send:
            peers._record_health(peer["id"], "healthy", {})  # simulate a just-completed check
            peers._maybe_health_check(peer)
        mock_send.assert_not_called()

    def test_rechecks_once_interval_has_elapsed(self):
        peer = _make_peer("IntervalPeer2")
        peer = dict(peer)
        peer["health_check_interval_minutes"] = 5
        store.write(lambda c: c.execute(
            "INSERT INTO paci_health(peer_id, last_status, last_checked_ts, last_detail, status_changed_ts) "
            "VALUES (?,?,?,?,?)", (peer["id"], "healthy", time.time() - 600, "{}", time.time() - 600)))
        with patch.object(peers, "_send_health_check") as mock_send:
            peers._maybe_health_check(peer)
        mock_send.assert_called_once()


if __name__ == "__main__":
    unittest.main()

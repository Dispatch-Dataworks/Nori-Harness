# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Real store, real crypto, real oauth.py -- only the actual network call
(urllib.request.urlopen) is faked. Covers connected_accounts.py's own
token-refresh/error-legibility logic (2026-09-18): the piece flagged
as the one most likely to break quietly months from now, so it gets a
real test rather than only a live throwaway-instance check.

A scratch NORI_DATA_DIR (own temp dir per test, removed after) rather
than mocking store.read/write -- this is the one nori test file that
touches a real (throwaway) sqlite file and a real Fernet key, since the
whole point is proving the refresh logic against real storage/
encryption, not a stand-in for it.
"""
import io
import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_connected_accounts_")
os.environ["NORI_DATA_DIR"] = _SCRATCH

import accounts  # noqa: E402
import connected_accounts  # noqa: E402
import store  # noqa: E402


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    payload = json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError("https://example.invalid", code, "err", {}, io.BytesIO(payload))


class _FakeResponse:
    def __init__(self, body):
        self._payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


class ConnectedAccountsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init()
        user = accounts.bootstrap_admin("Tester", "testpass123")
        cls.user_id = user["id"]
        # Shared across all four Google providers now (2026-09-18, his own
        # env naming) -- gmail/google_calendar/google_contacts/google_drive
        # all read this same pair, not a per-provider one.
        os.environ["GOOGLE_CLIENT_ID"] = "test-client-id"
        os.environ["GOOGLE_CLIENT_SECRET"] = "test-client-secret"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_SCRATCH, ignore_errors=True)

    def _connect(self, provider="gmail", access_token="old-access", refresh_token="real-refresh",
                expires_in=-10):
        connected_accounts.start_pending(self.user_id, provider, "state")
        connected_accounts.store_tokens(self.user_id, provider, access_token, refresh_token, expires_in)

    def test_fresh_token_returned_without_any_network_call(self):
        self._connect(expires_in=3600)
        with patch("urllib.request.urlopen") as m:
            result = connected_accounts.get_valid_access_token(self.user_id, "gmail")
        m.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["access_token"], "old-access")

    def test_expired_token_refreshes_and_does_not_clobber_the_refresh_token(self):
        self._connect(expires_in=-10, refresh_token="keep-me")
        with patch("urllib.request.urlopen", return_value=_FakeResponse(
                {"access_token": "new-access", "expires_in": 3600})):
            result = connected_accounts.get_valid_access_token(self.user_id, "gmail")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["access_token"], "new-access")
        row = connected_accounts.get(self.user_id, "gmail")
        self.assertEqual(row["status"], "connected")
        self.assertEqual(connected_accounts.real_access_token(row), "new-access")
        # The real point of this test: a refresh response with no
        # refresh_token field (Google's normal shape) must never wipe out
        # the one already stored.
        self.assertEqual(connected_accounts.real_refresh_token(row), "keep-me")

    def test_revoked_refresh_token_marks_needs_reconnect_with_specific_reason(self):
        self._connect(expires_in=-10)
        with patch("urllib.request.urlopen",
                  side_effect=_http_error(400, {"error": "invalid_grant",
                                                "error_description": "Token has been expired or revoked."})):
            result = connected_accounts.get_valid_access_token(self.user_id, "gmail")
        self.assertFalse(result["ok"])
        self.assertIn("revoked", result["error"])
        self.assertIn("reconnect", result["error"])
        row = connected_accounts.get(self.user_id, "gmail")
        self.assertEqual(row["status"], "needs_reconnect")
        reason = json.loads(row["meta"])["needs_reconnect_reason"]
        self.assertIn("invalid_grant", reason)

    def test_transient_refresh_failure_leaves_status_connected(self):
        self._connect(expires_in=-10)
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timed out")):
            result = connected_accounts.get_valid_access_token(self.user_id, "gmail")
        self.assertFalse(result["ok"])
        self.assertIn("try again shortly", result["error"])
        row = connected_accounts.get(self.user_id, "gmail")
        # Unlike a revoked grant, a network blip shouldn't force a real
        # reconnect -- the same refresh token might just work next time.
        self.assertEqual(row["status"], "connected")

    def test_expired_with_no_refresh_token_gives_a_specific_message(self):
        self._connect(expires_in=-10, refresh_token=None)
        result = connected_accounts.get_valid_access_token(self.user_id, "gmail")
        self.assertFalse(result["ok"])
        self.assertIn("no refresh token", result["error"])
        self.assertEqual(connected_accounts.get(self.user_id, "gmail")["status"], "needs_reconnect")

    def test_never_connected_says_so_plainly(self):
        connected_accounts.disconnect(self.user_id, "outlook")
        result = connected_accounts.get_valid_access_token(self.user_id, "outlook")
        self.assertFalse(result["ok"])
        self.assertIn("isn't connected", result["error"])

    def test_authed_request_401_marks_needs_reconnect(self):
        self._connect(expires_in=3600)  # locally-fresh -- the API itself says otherwise
        with patch("urllib.request.urlopen", side_effect=_http_error(
                401, {"error": {"code": 401, "message": "Invalid Credentials", "status": "UNAUTHENTICATED"}})):
            result = connected_accounts.authed_request(self.user_id, "gmail", "https://example.invalid/x")
        self.assertFalse(result["ok"])
        self.assertIn("revoked", result["error"])
        self.assertIn("Invalid Credentials", result["error"])
        self.assertEqual(connected_accounts.get(self.user_id, "gmail")["status"], "needs_reconnect")

    def test_authed_request_403_names_the_real_reason(self):
        self._connect(expires_in=3600)
        with patch("urllib.request.urlopen", side_effect=_http_error(403, {"error": {
                "code": 403, "status": "PERMISSION_DENIED",
                "message": "Gmail API has not been used in project 123 before or it is disabled."}})):
            result = connected_accounts.authed_request(self.user_id, "gmail", "https://example.invalid/x")
        self.assertFalse(result["ok"])
        self.assertIn("has not been used in project 123", result["error"])
        # A disabled API isn't a revoked connection -- must not flip status.
        self.assertEqual(connected_accounts.get(self.user_id, "gmail")["status"], "connected")

    def test_authed_request_success_returns_real_data(self):
        self._connect(expires_in=3600)
        with patch("urllib.request.urlopen", return_value=_FakeResponse({"messages": [{"id": "abc"}]})):
            result = connected_accounts.authed_request(self.user_id, "gmail", "https://example.invalid/x")
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["messages"], [{"id": "abc"}])

    def test_authed_request_raw_response_skips_json_parsing(self):
        # Drive's files.get?alt=media/export return real file bytes, not
        # JSON -- json.loads()'ing that would crash, not just misbehave.
        self._connect(provider="google_drive", expires_in=3600)
        with patch("urllib.request.urlopen", return_value=_FakeResponse(b"not json, just text content")):
            result = connected_accounts.authed_request(
                self.user_id, "google_drive", "https://example.invalid/x", raw_response=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], b"not json, just text content")

    def test_authed_request_raw_bytes_body_sent_as_is(self):
        # Drive's multipart upload sends a hand-built multipart/related
        # body, not JSON -- must go out byte-for-byte, not re-encoded.
        self._connect(provider="google_drive", expires_in=3600)
        captured = {}

        def _fake_urlopen(req, timeout=20):
            captured["data"] = req.data
            captured["content_type"] = req.get_header("Content-type")
            return _FakeResponse({"id": "new-file-id"})

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = connected_accounts.authed_request(
                self.user_id, "google_drive", "https://example.invalid/upload", method="POST",
                body=b"raw-multipart-bytes", content_type="multipart/related; boundary=abc")
        self.assertTrue(result["ok"])
        self.assertEqual(captured["data"], b"raw-multipart-bytes")
        self.assertEqual(captured["content_type"], "multipart/related; boundary=abc")

    def test_peer_gates(self):
        self.assertTrue(connected_accounts.peer_blocked({}))
        self.assertFalse(connected_accounts.peer_blocked({"_peer_context": "TestPeer"}))
        self.assertTrue(connected_accounts.peer_trust_gate({}))
        self.assertFalse(connected_accounts.peer_trust_gate({"_peer_context": "TestPeer", "_peer_trust": "prompt"}))
        self.assertTrue(connected_accounts.peer_trust_gate({"_peer_context": "TestPeer", "_peer_trust": "full"}))


class SchedulerSignalTests(unittest.TestCase):
    """The proactive "a connected account needs reconnecting" ping
    (2026-09-27, real incident: four of the operator's own Google
    accounts died the same week and nothing ever told him)."""

    @classmethod
    def setUpClass(cls):
        store.init()
        user = accounts.bootstrap_admin("Tester2", "testpass123")
        cls.user_id = user["id"]
        os.environ["GOOGLE_CLIENT_ID"] = "test-client-id"
        os.environ["GOOGLE_CLIENT_SECRET"] = "test-client-secret"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_SCRATCH, ignore_errors=True)

    def tearDown(self):
        for provider in ("gmail", "google_calendar", "outlook"):
            connected_accounts.disconnect(self.user_id, provider)

    def _connect_and_break(self, provider, reason, age_days=None):
        connected_accounts.start_pending(self.user_id, provider, "state")
        connected_accounts.store_tokens(self.user_id, provider, "old-access", "real-refresh", 3600)
        connected_accounts.mark_needs_reconnect(self.user_id, provider, reason)
        if age_days is not None:
            backdated = time.time() - age_days * 86400
            store.write(lambda c: c.execute(
                "UPDATE connected_accounts SET connected_ts=? WHERE user_id=? AND provider=?",
                (backdated, self.user_id, provider)))

    def test_nothing_broken_returns_none(self):
        self.assertIsNone(connected_accounts.scheduler_signal(self.user_id))

    def test_one_broken_account_is_named_with_its_reason(self):
        self._connect_and_break("gmail", "some other real error", age_days=1)
        result = connected_accounts.scheduler_signal(self.user_id)
        self.assertIsNotNone(result)
        message, dedup = result
        self.assertIn("Gmail", message)
        self.assertIn("some other real error", message)
        self.assertEqual(dedup, "gmail")

    def test_two_broken_accounts_both_named_dedup_is_the_combined_set(self):
        self._connect_and_break("gmail", "invalid_grant: bad", age_days=1)
        self._connect_and_break("google_calendar", "invalid_grant: bad", age_days=1)
        message, dedup = connected_accounts.scheduler_signal(self.user_id)
        self.assertIn("Gmail", message)
        self.assertIn("Calendar", message)
        self.assertEqual(dedup, "gmail,google_calendar")

    def test_testing_status_heuristic_fires_inside_the_window(self):
        self._connect_and_break("gmail", "invalid_grant: Token has been expired or revoked.", age_days=10)
        message, _ = connected_accounts.scheduler_signal(self.user_id)
        self.assertIn("Testing", message)
        self.assertIn("google-and-microsoft.md", message)

    def test_testing_status_heuristic_does_not_fire_before_seven_days(self):
        self._connect_and_break("gmail", "invalid_grant: Token has been expired or revoked.", age_days=2)
        message, _ = connected_accounts.scheduler_signal(self.user_id)
        self.assertNotIn("Testing", message)

    def test_testing_status_heuristic_does_not_fire_past_the_generous_upper_bound(self):
        self._connect_and_break("gmail", "invalid_grant: Token has been expired or revoked.", age_days=200)
        message, _ = connected_accounts.scheduler_signal(self.user_id)
        self.assertNotIn("Testing", message)

    def test_testing_status_heuristic_does_not_fire_for_a_non_google_provider(self):
        self._connect_and_break("outlook", "invalid_grant: Token has been expired or revoked.", age_days=10)
        message, _ = connected_accounts.scheduler_signal(self.user_id)
        self.assertNotIn("Testing", message)

    def test_malformed_meta_does_not_crash_the_signal(self):
        self._connect_and_break("gmail", "some error", age_days=1)
        store.write(lambda c: c.execute(
            "UPDATE connected_accounts SET meta=? WHERE user_id=? AND provider=?",
            ("not valid json", self.user_id, "gmail")))
        message, dedup = connected_accounts.scheduler_signal(self.user_id)
        self.assertIn("Gmail", message)
        self.assertEqual(dedup, "gmail")

    def test_costs_nothing_no_network_call_regardless_of_how_many_are_broken(self):
        self._connect_and_break("gmail", "invalid_grant: bad", age_days=10)
        self._connect_and_break("google_calendar", "invalid_grant: bad", age_days=10)
        with patch("urllib.request.urlopen") as m:
            connected_accounts.scheduler_signal(self.user_id)
        m.assert_not_called()

    def test_registered_with_the_others(self):
        import scheduler
        keys = [s["key"] for s in scheduler.signal_registry()]
        self.assertIn("account_needs_reconnect", keys)


if __name__ == "__main__":
    unittest.main()

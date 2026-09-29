# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""peer_turn_limit_log (2026-09-19, real gap found investigating
unexplained silence toward a peer): a peer-motivated turn that exhausts
its own round budget used to leave no durable, attributable record --
this is the wiring test confirming peers._run_prompted_turn actually
writes one when chat.run() reports hit_round_limit=True, and does NOT
when it doesn't. chat.run is mocked (no live model endpoint in this
harness); turns.run() is real."""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_turn_limit_log_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import chat  # noqa: E402
import peers  # noqa: E402
import store  # noqa: E402
import turns  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")


def _make_peer(name):
    result = peers.create_peer(
        {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"},
        scope="user", name=name, url=f"https://example.invalid/paci/inbound/{name}",
        psk="a-real-shared-secret")
    assert result.get("ok"), result
    return peers.get_peer(result["peer_id"])


def _log_rows(peer_id):
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT * FROM peer_turn_limit_log WHERE peer_id=? ORDER BY id", (peer_id,)).fetchall())]


def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class TurnLimitLoggingTests(unittest.TestCase):
    def test_hit_round_limit_gets_logged(self):
        # The patch context must stay open until the background thread
        # actually calls chat.run() -- force_checkin() itself returns as
        # soon as the thread is STARTED, well before that happens, so
        # the wait for the logged row has to live INSIDE the `with`, or
        # the mock reverts to the real chat.run (a real network call,
        # ModelError, no log row) before the thread ever reaches it.
        peer = _make_peer(f"limit-hit-{time.time()}")
        session = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"}
        with patch.object(peers.chat, "run", return_value={
                "text": "(hit the 4-step tool-call limit...)",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "hit_round_limit": True}):
            result = peers.force_checkin(session, peer["id"])
            self.assertTrue(result.get("ok"), result)
            self.assertTrue(_wait_until(lambda: len(_log_rows(peer["id"])) == 1))
        row = _log_rows(peer["id"])[0]
        self.assertEqual(row["peer_id"], peer["id"])
        self.assertIn("check in now", row["reason"])
        self.assertGreater(row["rounds"], 0)

    def test_normal_completion_logs_nothing(self):
        peer = _make_peer(f"limit-clean-{time.time()}")
        session = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"}
        with patch.object(peers.chat, "run", return_value={
                "text": "All good, nothing to report.",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "hit_round_limit": False}):
            result = peers.force_checkin(session, peer["id"])
            self.assertTrue(result.get("ok"), result)
            # No log row is the expected, permanent state here -- wait for
            # the turn to actually finish (via the lock clearing) rather
            # than a fixed sleep, then assert nothing was logged.
            self.assertTrue(_wait_until(lambda: not turns.in_flight(_user["id"])))
        self.assertEqual(_log_rows(peer["id"]), [])


def _model_failure_rows(peer_id):
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT * FROM peer_model_failure_log WHERE peer_id=? ORDER BY id", (peer_id,)).fetchall())]


class ModelFailureLoggingTests(unittest.TestCase):
    """peer_model_failure_log (2026-09-19) -- a different failure mode
    from the round-limit table above: the model call itself failed
    outright, after chat.py's own retries, before any round loop ran to
    completion. Same "ensure silence is truly her choice" sweep."""

    def test_model_error_gets_logged(self):
        peer = _make_peer(f"model-fail-{time.time()}")
        session = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"}
        with patch.object(peers.chat, "run", side_effect=chat.ModelError("model call failed after 3 attempt(s)")):
            result = peers.force_checkin(session, peer["id"])
            self.assertTrue(result.get("ok"), result)
            self.assertTrue(_wait_until(lambda: len(_model_failure_rows(peer["id"])) == 1))
        row = _model_failure_rows(peer["id"])[0]
        self.assertEqual(row["peer_id"], peer["id"])
        self.assertIn("model call failed", row["error"])
        self.assertIn("check in now", row["reason"])

    def test_normal_completion_logs_no_model_failure(self):
        peer = _make_peer(f"model-ok-{time.time()}")
        session = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"}
        with patch.object(peers.chat, "run", return_value={
                "text": "All good.", "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "hit_round_limit": False}):
            result = peers.force_checkin(session, peer["id"])
            self.assertTrue(result.get("ok"), result)
            self.assertTrue(_wait_until(lambda: not turns.in_flight(_user["id"])))
        self.assertEqual(_model_failure_rows(peer["id"]), [])


class DiagnosticsTimelineTests(unittest.TestCase):
    """diagnostics.events(): the one merged "why was there no reply / why was
    it down" timeline (2026-09-18)."""

    def _insert(self, sql, params):
        store.write(lambda c: c.execute(sql, params))

    def test_every_source_lands_in_one_newest_first_timeline(self):
        import json
        import diagnostics
        peer = _make_peer(f"diag-{time.time()}")
        pid, now = peer["id"], time.time()
        self._insert("INSERT INTO peer_compulsion_log(ts, peer_id, message_ids, decision, detail) VALUES (?,?,?,?,?)",
                     (now - 50, pid, json.dumps([1, 2]), "dropped_expired", "waited too long"))
        self._insert("INSERT INTO peer_turn_limit_log(ts, peer_id, rounds, reason) VALUES (?,?,?,?)",
                     (now - 40, pid, 12, "a nudge"))
        self._insert("INSERT INTO peer_model_failure_log(ts, peer_id, error, reason) VALUES (?,?,?,?)",
                     (now - 30, pid, "model call failed after 3 attempt(s): timed out", "a nudge"))
        self._insert("INSERT INTO messages(user_id, ts, role, content, kind, meta) VALUES (?,?,?,?,?,?)",
                     (_user["id"], now - 20, "assistant", f"used peer{pid}_send", "tool",
                      json.dumps({"tool_name": f"peer{pid}_send", "failed": True, "error": "peer refused: rate limit"})))
        # a NON-failed peer tool call and a failed non-peer tool must not appear
        self._insert("INSERT INTO messages(user_id, ts, role, content, kind, meta) VALUES (?,?,?,?,?,?)",
                     (_user["id"], now - 19, "assistant", f"used peer{pid}_send", "tool",
                      json.dumps({"tool_name": f"peer{pid}_send"})))
        self._insert("INSERT INTO messages(user_id, ts, role, content, kind, meta) VALUES (?,?,?,?,?,?)",
                     (_user["id"], now - 18, "assistant", "used web_search", "tool",
                      json.dumps({"tool_name": "web_search", "failed": True, "error": "x"})))
        sup = os.path.join(_SCRATCH, "sup.jsonl")
        with open(sup, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 10)),
                                "kind": "ensure", "caller": "ensure-all", "ok": False,
                                "detail": "was not serving; restart did NOT bring it up",
                                "crash": "ImportError: boom"}) + "\n")
            f.write("not json at all\n")  # a torn line must not break the page
        evs = [e for e in diagnostics.events(50, supervision_path=sup) if e["peer"] in (peer["name"], None)]
        by_source = {e["source"]: e for e in evs}
        self.assertEqual(set(by_source), {"reply_requested", "round limit", "model call", "peer tool", "supervisor"})
        self.assertEqual([e["source"] for e in evs],
                         ["supervisor", "peer tool", "model call", "round limit", "reply_requested"])
        self.assertFalse(by_source["reply_requested"]["ok"])
        self.assertIn("2 messages", by_source["reply_requested"]["title"])
        self.assertIn("timed out", by_source["model call"]["detail"])
        self.assertIn("rate limit", by_source["peer tool"]["detail"])
        self.assertIn("ImportError: boom", by_source["supervisor"]["detail"])
        self.assertEqual(sum(1 for e in evs if e["source"] == "peer tool"), 1)

    def test_missing_supervision_log_is_just_empty(self):
        import diagnostics
        evs = diagnostics.events(5, supervision_path=os.path.join(_SCRATCH, "no-such-file.jsonl"))
        self.assertFalse([e for e in evs if e["source"] == "supervisor"])


if __name__ == "__main__":
    unittest.main()

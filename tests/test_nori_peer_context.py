# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Peer-context visibility (2026-09-19, operator's own answer to "are peer
messages actually in context on an ordinary turn," plus his own follow-up
refinement the same day): real gaps found and fixed together, each
exercised against the real DB, no mocking of peers.py/context.py's own
query logic --

1. peers.recent_context_block() used to be received-only (`direction=
   'received'`) -- she could see an incoming peer message with no memory
   of having already answered it. Fixed to include her own sent messages
   too, attributed "you told X" rather than "X told you".
2. The same function's cap used to be shared ACROSS every connected peer
   combined -- one chatty peer could evict another's messages regardless
   of recency. Fixed to a per-peer cap via a windowed SQL query.
3. Read state is two-dimensional, not one flag ("read in a peer initiated
   turn is not read in a user initiated turn," operator's own words):
   presented_peer_ts and presented_user_ts are independent columns,
   independently stamped by pending_delivery_messages(dimension=...).
   recent_context_block() gates on presented_user_ts specifically (it's a
   mechanism for turns talking to HIM), never presented_peer_ts."""
import json
import os
import sys
import tempfile
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_peer_context_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_peer_context_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import config  # noqa: E402
import context  # noqa: E402
import peers  # noqa: E402
import store  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")


def _make_peer(name):
    result = peers.create_peer(
        {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"},
        scope="user", name=name, url=f"https://example.invalid/paci/inbound/{name}",
        psk="a-real-shared-secret")
    assert result.get("ok"), result
    return peers.get_peer(result["peer_id"])


def _insert_received(peer_id, text, *, ts=None, presented_peer=True, presented_user=True,
                     suspicious=False):
    ts = time.time() if ts is None else ts
    body = {"content": text, "suspicious": suspicious}
    store.write(lambda c: c.execute(
        "INSERT INTO peer_messages(peer_id, conversation_id, message_id, direction, type, seq, "
        "body_json, ts, status, presented_peer_ts, presented_user_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (peer_id, str(uuid.uuid4()), str(uuid.uuid4()), "received", "message", 1,
         json.dumps(body), ts, "delivered",
         ts if presented_peer else None, ts if presented_user else None)))


def _insert_sent(peer_id, text, *, ts=None):
    ts = time.time() if ts is None else ts
    store.write(lambda c: c.execute(
        "INSERT INTO peer_messages(peer_id, conversation_id, message_id, direction, type, seq, "
        "body_json, ts, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (peer_id, str(uuid.uuid4()), str(uuid.uuid4()), "sent", "message", 1,
         json.dumps({"text": text}), ts, "pending")))


def _row(peer_id):
    return dict(store.read(lambda c: c.execute(
        "SELECT presented_peer_ts, presented_user_ts FROM peer_messages "
        "WHERE peer_id=? AND direction='received'", (peer_id,)).fetchone()))


class RecentContextBlockTests(unittest.TestCase):
    """recent_context_block() gates on presented_user_ts specifically --
    it's the background-awareness block that appears on turns talking to
    HIM, so a received row only belongs here once it's actually been
    shown to that dimension, regardless of presented_peer_ts."""

    def setUp(self):
        self.peer_a = _make_peer(f"peer-a-{uuid.uuid4().hex[:8]}")
        self.peer_b = _make_peer(f"peer-b-{uuid.uuid4().hex[:8]}")
        config.set("workspace", _user["workspace_id"], "peer_recent_cap", 2)
        config.set("workspace", _user["workspace_id"], "peer_recent_window_hours", 12)

    def test_includes_both_directions_with_correct_attribution(self):
        _insert_received(self.peer_a["id"], "how's the weather over there")
        _insert_sent(self.peer_a["id"], "rainy, why do you ask")
        block = peers.recent_context_block(_user["id"])
        self.assertIn(f'{self.peer_a["name"]} told you', block)
        self.assertIn("how's the weather over there", block)
        self.assertIn(f'you told {self.peer_a["name"]}', block)
        self.assertIn("rainy, why do you ask", block)

    def test_sent_message_never_flagged_suspicious(self):
        _insert_sent(self.peer_a["id"], "a note she sent herself")
        block = peers.recent_context_block(_user["id"])
        self.assertIn("a note she sent herself", block)
        self.assertNotIn("flagged suspicious", block)

    def test_unpresented_received_row_excluded(self):
        _insert_received(self.peer_a["id"], "still unread", presented_peer=False, presented_user=False)
        block = peers.recent_context_block(_user["id"])
        self.assertNotIn("still unread", block)

    def test_peer_presented_but_not_user_presented_still_excluded(self):
        # The exact case the operator's own correction is about: shown to a
        # peer-motivated turn (presented_peer_ts set) does NOT count as
        # read for this block, which cares about presented_user_ts only.
        _insert_received(self.peer_a["id"], "seen by a peer turn only",
                         presented_peer=True, presented_user=False)
        block = peers.recent_context_block(_user["id"])
        self.assertNotIn("seen by a peer turn only", block)

    def test_cap_is_per_peer_not_shared_across_peers(self):
        # cap=2 (setUp). Flood peer_a with 5 recent messages; peer_b gets
        # exactly one. Under the old shared-cap bug, peer_b's one message
        # would have been evicted by peer_a's flood. Under the fix, each
        # peer is capped independently.
        now = time.time()
        for i in range(5):
            _insert_received(self.peer_a["id"], f"flood message {i}", ts=now - i)
        _insert_received(self.peer_b["id"], "peer b's one message", ts=now - 1)
        block = peers.recent_context_block(_user["id"])
        self.assertIn("peer b's one message", block)
        peer_a_hits = sum(1 for i in range(5) if f"flood message {i}" in block)
        self.assertEqual(peer_a_hits, 2, f"expected peer_a capped at 2, got {peer_a_hits}:\n{block}")

    def test_window_hours_excludes_old_messages(self):
        now = time.time()
        _insert_received(self.peer_a["id"], "an old exchange", ts=now - 13 * 3600)  # window is 12h
        _insert_received(self.peer_a["id"], "a fresh exchange", ts=now - 1 * 3600)
        block = peers.recent_context_block(_user["id"])
        self.assertNotIn("an old exchange", block)
        self.assertIn("a fresh exchange", block)


class PendingDeliveryUnaffectedTests(unittest.TestCase):
    """Sanity check: the unread/pending-content path (build_messages'
    peer_pending param) is a completely different query (presented_*_ts
    IS NULL, per dimension) from recent_context_block's -- confirms the
    two never double-surface the same row."""

    def setUp(self):
        self.peer = _make_peer(f"peer-pending-{uuid.uuid4().hex[:8]}")

    def test_unread_message_injected_once_then_moves_to_recent_block(self):
        _insert_received(self.peer["id"], "a fresh unread ping", presented_peer=False, presented_user=False)
        msgs = context.build_messages(_user["id"], "Tester", peer_pending="user")
        self.assertIn("a fresh unread ping", json.dumps(msgs))
        # Second call: now presented for "user", so the text legitimately
        # still appears -- but only via the standing recent-context block
        # inside the system message (build_system() is unconditional), not
        # as a second trailing pending-content message. One fewer message
        # overall is the real signal: the pending block was dropped.
        msgs2 = context.build_messages(_user["id"], "Tester", peer_pending="user")
        self.assertEqual(len(msgs2), len(msgs) - 1)
        self.assertIn("a fresh unread ping", msgs2[0]["content"])  # via recent-context, in the system message
        self.assertNotIn("a fresh unread ping", json.dumps(msgs2[1:]))  # not re-injected as pending
        row = _row(self.peer["id"])
        self.assertIsNotNone(row["presented_user_ts"])

    def test_withheld_when_peer_pending_is_none(self):
        _insert_received(self.peer["id"], "should stay hidden this turn",
                         presented_peer=False, presented_user=False)
        msgs = context.build_messages(_user["id"], "Tester", peer_pending=None)
        self.assertNotIn("should stay hidden this turn", json.dumps(msgs))
        row = _row(self.peer["id"])
        self.assertIsNone(row["presented_user_ts"])  # withheld, not marked presented -- never lost
        self.assertIsNone(row["presented_peer_ts"])


class TwoDimensionalReadStateTests(unittest.TestCase):
    """The refinement asked for the same day: "read in a peer
    initiated turn is not read in a user initiated turn... those should
    still be unread until she sees them in context talking to me." Real
    exercise of both dimensions against the real DB -- not just a schema
    check, an actual double-call of pending_delivery_messages() with each
    dimension, in both orders."""

    def setUp(self):
        self.peer = _make_peer(f"peer-dims-{uuid.uuid4().hex[:8]}")

    def test_peer_turn_does_not_satisfy_user_dimension(self):
        _insert_received(self.peer["id"], "peer saw this first",
                         presented_peer=False, presented_user=False)
        peer_msgs = peers.pending_delivery_messages(_user["id"], dimension="peer")
        self.assertIn("peer saw this first", json.dumps(peer_msgs))
        row = _row(self.peer["id"])
        self.assertIsNotNone(row["presented_peer_ts"])
        self.assertIsNone(row["presented_user_ts"])  # still unread for "user"
        # A subsequent user-dimension call must STILL surface it.
        user_msgs = peers.pending_delivery_messages(_user["id"], dimension="user")
        self.assertIn("peer saw this first", json.dumps(user_msgs))
        row2 = _row(self.peer["id"])
        self.assertIsNotNone(row2["presented_user_ts"])

    def test_user_turn_does_not_satisfy_peer_dimension(self):
        # The symmetric case -- shown to him first, still pending for a
        # future peer-motivated turn.
        _insert_received(self.peer["id"], "he saw this first",
                         presented_peer=False, presented_user=False)
        user_msgs = peers.pending_delivery_messages(_user["id"], dimension="user")
        self.assertIn("he saw this first", json.dumps(user_msgs))
        row = _row(self.peer["id"])
        self.assertIsNotNone(row["presented_user_ts"])
        self.assertIsNone(row["presented_peer_ts"])
        peer_msgs = peers.pending_delivery_messages(_user["id"], dimension="peer")
        self.assertIn("he saw this first", json.dumps(peer_msgs))

    def test_once_both_dimensions_satisfied_neither_reinjects(self):
        _insert_received(self.peer["id"], "fully handled now",
                         presented_peer=False, presented_user=False)
        peers.pending_delivery_messages(_user["id"], dimension="peer")
        peers.pending_delivery_messages(_user["id"], dimension="user")
        self.assertEqual(peers.pending_delivery_messages(_user["id"], dimension="peer"), [])
        self.assertEqual(peers.pending_delivery_messages(_user["id"], dimension="user"), [])

    def test_invalid_dimension_rejected(self):
        with self.assertRaises(ValueError):
            peers.pending_delivery_messages(_user["id"], dimension="nonsense")


if __name__ == "__main__":
    unittest.main()

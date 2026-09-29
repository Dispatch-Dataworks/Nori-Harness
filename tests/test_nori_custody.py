# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Nori's custody of a sealed result for a peer (nori/custody.py, 2026-09-19).

The point of these tests is the operator's stated worry -- Nori not behaving well with secrets -- so they pin the STRUCTURE that makes a leak impossible rather than
unlikely: the payload is encrypted at rest and read by no module but custody.py (a source scan over every nori/*.py file, and over prompts and docs so it
cannot be handed to the model in text); the exchange never becomes a conversation or a message; release is by the clock alone and retried forever; the
payload is erased once acknowledged. Real HMAC verification; only the outbound network call is replaced."""
import json
import os
import re
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_custody_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import crypto  # noqa: E402
import custody  # noqa: E402
import peers  # noqa: E402
import store  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")
PSK = os.urandom(12).hex()                      # random per run: nothing here is a hardcoded credential
NORI_DIR = Path(ROOT)


def _tok(n):
    return f"tok-{n:016d}"


def _make_peer(name="TestPeer"):
    result = peers.create_peer({"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"},
                               scope="user", name=name, url="https://example.invalid/paci/inbound/peer", psk=PSK)
    assert result.get("ok"), result
    return peers.get_peer(result["peer_id"])


def _hold_envelope(token=_tok(0), ref="game7", reveal_ts=None, payload='{"salt":"s","winner_index":2}', commitment=None):
    return {"type": "custody_hold", "paci_version": "1.0",
            "body": {"token": token, "game_ref": ref, "reveal_ts": reveal_ts if reveal_ts is not None else time.time() + 3600,
                     "commitment": commitment or custody.commitment_of(ref, token, payload), "payload": payload}}


_n = [0]


def _deliver(peer, envelope, psk=PSK):
    raw = json.dumps(envelope).encode("utf-8")
    ts = str(time.time())
    _n[0] += 1
    nonce = f"nonce-{_n[0]}-0123456789abcdef"
    headers = {"X-PACI-Timestamp": ts, "X-PACI-Nonce": nonce, "X-PACI-Signature": peers._sign(psk, "POST", "/x", ts, nonce, raw)}
    return peers.handle_inbound(peer["id"], method="POST", path="/x", headers=headers, raw_body=raw)


def _row(token):
    return store.read(lambda c: c.execute("SELECT * FROM custody_holds WHERE token=?", (token,)).fetchone())


class HoldTests(unittest.TestCase):
    def test_a_signed_hold_is_stored_encrypted_and_acknowledged(self):
        peer = _make_peer("Hold1")
        env = _hold_envelope(token=_tok(1))
        status, resp = _deliver(peer, env)
        self.assertEqual(status, 200, resp)
        self.assertEqual(resp["type"], "custody_hold_ack")
        self.assertEqual(resp["body"]["commitment"], env["body"]["commitment"])
        row = _row(_tok(1))
        self.assertEqual(row["status"], "holding")
        self.assertNotIn("winner_index", row["payload_enc"])                           # NOT plaintext at rest
        self.assertNotIn("winner_index", json.dumps(dict(row)))
        self.assertEqual(json.loads(crypto.decrypt(row["payload_enc"]))["winner_index"], 2)

    def test_the_raw_database_file_does_not_contain_the_plaintext_payload(self):
        peer = _make_peer("Hold2")
        marker = '{"salt":"UNIQUE-MARKER-XYZ-987","winner_index":1}'
        self.assertEqual(_deliver(peer, _hold_envelope(token=_tok(2), payload=marker))[0], 200)
        blob = b"".join(p.read_bytes() for p in Path(_SCRATCH).rglob("*") if p.is_file() and p.suffix in (".db", ".sqlite", ".wal", "") and p.stat().st_size < 50_000_000
                        and p.name != "secret.key")
        self.assertNotIn(b"UNIQUE-MARKER-XYZ-987", blob)

    def test_a_retried_hold_is_acknowledged_and_never_stored_twice(self):
        peer = _make_peer("Hold3")
        env = _hold_envelope(token=_tok(3))
        self.assertEqual(_deliver(peer, env)[0], 200)
        self.assertEqual(_deliver(peer, env)[0], 200)
        self.assertEqual(store.read(lambda c: c.execute("SELECT count(*) FROM custody_holds WHERE token=?", (_tok(3),)).fetchone()[0]), 1)

    def test_the_same_token_with_a_different_commitment_is_refused(self):
        peer = _make_peer("Hold4")
        self.assertEqual(_deliver(peer, _hold_envelope(token=_tok(4)))[0], 200)
        other = _hold_envelope(token=_tok(4), payload='{"salt":"t","winner_index":0}')
        self.assertEqual(_deliver(peer, other)[0], 409)

    def test_a_payload_that_does_not_match_its_commitment_is_refused_and_not_stored(self):
        peer = _make_peer("Hold5")
        env = _hold_envelope(token=_tok(5))
        env["body"]["payload"] = '{"salt":"s","winner_index":0}'
        self.assertEqual(_deliver(peer, env)[0], 400)
        self.assertIsNone(_row(_tok(5)))

    def test_malformed_holds_are_refused(self):
        peer = _make_peer("Hold6")
        for mutate in (lambda b: b.pop("token"), lambda b: b.update(token="short"), lambda b: b.update(payload=""), lambda b: b.update(payload="x" * 5000),
                       lambda b: b.update(reveal_ts="soon"), lambda b: b.pop("commitment")):
            env = _hold_envelope(token=_tok(6))
            mutate(env["body"])
            self.assertEqual(_deliver(peer, env)[0], 400)

    def test_a_bad_signature_is_refused_before_anything_is_stored(self):
        peer = _make_peer("Hold7")
        status, _ = _deliver(peer, _hold_envelope(token=_tok(7)), psk="the-wrong-secret")
        self.assertEqual(status, 401)
        self.assertIsNone(_row(_tok(7)))

    def test_the_exchange_never_becomes_a_conversation_or_a_message(self):
        peer = _make_peer("Hold8")
        self.assertEqual(_deliver(peer, _hold_envelope(token=_tok(8)))[0], 200)
        self.assertEqual(store.read(lambda c: c.execute("SELECT count(*) FROM peer_conversations WHERE peer_id=?", (peer["id"],)).fetchone()[0]), 0)
        self.assertEqual(store.read(lambda c: c.execute("SELECT count(*) FROM peer_messages WHERE peer_id=?", (peer["id"],)).fetchone()[0]), 0)


class ReleaseTests(unittest.TestCase):
    def hold(self, name, token, reveal_ts):
        peer = _make_peer(name)
        self.assertEqual(_deliver(peer, _hold_envelope(token=token, reveal_ts=reveal_ts))[0], 200)
        return peer

    def test_nothing_is_released_before_its_time_and_it_is_released_by_the_clock_after(self):
        self.hold("Rel1", "tok-rel-aaaaaaaaa1", time.time() + 3600)
        sent = []
        with patch.object(peers, "_post", lambda p, e: sent.append(e) or {"type": "custody_release_ack"}):
            custody.tick()
            self.assertEqual(sent, [])
            store.write(lambda c: c.execute("UPDATE custody_holds SET reveal_ts=? WHERE token=?", (time.time() - 1, "tok-rel-aaaaaaaaa1")))
            custody.tick()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["type"], "custody_release")
        self.assertEqual(sent[0]["body"]["token"], "tok-rel-aaaaaaaaa1")
        self.assertEqual(json.loads(sent[0]["body"]["payload"])["winner_index"], 2)

    def test_once_acknowledged_the_payload_is_erased_and_it_is_not_sent_again(self):
        self.hold("Rel2", "tok-rel-aaaaaaaaa2", time.time() - 5)
        sent = []
        with patch.object(peers, "_post", lambda p, e: sent.append(e) or {"type": "custody_release_ack"}):
            custody.tick()
            custody.tick()
        self.assertEqual(len([e for e in sent if e["body"]["token"] == "tok-rel-aaaaaaaaa2"]), 1)
        row = _row("tok-rel-aaaaaaaaa2")
        self.assertEqual(row["status"], "released")
        self.assertIsNone(row["payload_enc"])

    def test_an_unreachable_owner_is_retried_every_tick_and_nothing_expires(self):
        self.hold("Rel3", "tok-rel-aaaaaaaaa3", time.time() - 10 * 86400)                 # ten days overdue
        def boom(p, e):
            raise urllib.error.URLError("peer is down")
        with patch.object(peers, "_post", boom):
            for _ in range(5):
                custody.tick()
        row = _row("tok-rel-aaaaaaaaa3")
        self.assertEqual(row["status"], "holding")                                         # still there, still waiting
        self.assertGreaterEqual(row["attempts"], 5)
        self.assertIn("peer is down", row["last_error"])
        self.assertIsNotNone(row["payload_enc"])
        with patch.object(peers, "_post", lambda p, e: {"type": "custody_release_ack"}):
            custody.tick()                                                                 # she is back: it lands
        self.assertEqual(_row("tok-rel-aaaaaaaaa3")["status"], "released")

    def test_a_refusal_from_the_owner_is_not_an_acknowledgement(self):
        self.hold("Rel4", "tok-rel-aaaaaaaaa4", time.time() - 5)
        with patch.object(peers, "_post", lambda p, e: {"type": "error", "body": {"reason": "games are switched off here"}}):
            custody.tick()
        row = _row("tok-rel-aaaaaaaaa4")
        self.assertEqual(row["status"], "holding")
        self.assertIn("switched off", row["last_error"])

    def test_a_disabled_peer_does_not_lose_the_hold(self):
        peer = self.hold("Rel5", "tok-rel-aaaaaaaaa5", time.time() - 5)
        store.write(lambda c: c.execute("UPDATE peers SET enabled=0 WHERE id=?", (peer["id"],)))
        custody.tick()
        self.assertEqual(_row("tok-rel-aaaaaaaaa5")["status"], "holding")
        store.write(lambda c: c.execute("UPDATE peers SET enabled=1 WHERE id=?", (peer["id"],)))
        with patch.object(peers, "_post", lambda p, e: {"type": "custody_release_ack"}):
            custody.tick()
        self.assertEqual(_row("tok-rel-aaaaaaaaa5")["status"], "released")

    def test_one_failing_hold_does_not_stop_the_others(self):
        self.hold("Rel6", "tok-rel-aaaaaaaaa6", time.time() - 20)
        self.hold("Rel7", "tok-rel-aaaaaaaaa7", time.time() - 10)
        calls = []

        def flaky(p, e):
            calls.append(e["body"]["token"])
            if e["body"]["token"] == "tok-rel-aaaaaaaaa6":
                raise RuntimeError("boom")
            return {"type": "custody_release_ack"}
        with patch.object(peers, "_post", flaky):
            custody.tick()
        self.assertEqual(_row("tok-rel-aaaaaaaaa7")["status"], "released")
        self.assertEqual(_row("tok-rel-aaaaaaaaa6")["status"], "holding")

    def test_it_is_driven_by_the_peers_tick_and_survives_a_restart(self):
        self.hold("Rel8", "tok-rel-aaaaaaaaa8", time.time() - 5)
        import importlib
        importlib.reload(custody)                                                          # a fresh process: nothing in memory
        with patch.object(peers, "_post", lambda p, e: {"type": "custody_release_ack"}):
            peers.tick()
        self.assertEqual(_row("tok-rel-aaaaaaaaa8")["status"], "released")


class NoPathToTheModelTests(unittest.TestCase):
    """The structural guarantee: nothing but custody.py (and the schema that creates the table) so much as names the table or its module."""

    @staticmethod
    def scan(root=NORI_DIR):
        hits = {}
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix not in (".py", ".md", ".txt", ".html", ".json", ".yaml", ".yml", ".ps1") or "__pycache__" in p.parts or "data" in p.parts[len(root.parts):] or "tests" in p.parts[len(root.parts):]:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if re.search(r"custody_holds|\bimport custody\b|\bfrom custody\b", text):
                hits[p.name] = True
        return hits

    def test_only_custody_the_schema_the_inbound_hook_and_the_docs_touch_it(self):
        allowed = {"custody.py", "store.py", "peers.py"}
        self.assertEqual(set(self.scan()) - allowed, set(), "something else names the custody table / module: a path to the model")

    def test_peers_only_delegates_and_never_reads_the_table(self):
        src = (NORI_DIR / "peers.py").read_text(encoding="utf-8")
        self.assertNotIn("custody_holds", src)
        self.assertEqual(len(re.findall(r"import custody", src)), 2)                       # the inbound hook and the tick, nothing else

    def test_no_tool_or_context_or_chat_module_imports_it(self):
        for name in ("tools.py", "context.py", "chat.py", "server.py", "memory.py", "precheck.py", "conversation.py", "turns.py", "scheduler.py", "settings_tool.py"):
            self.assertNotRegex((NORI_DIR / name).read_text(encoding="utf-8"), r"custody", name)

    def test_the_scan_itself_can_fail(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "sneaky_tool.py").write_text("rows = c.execute('SELECT payload_enc FROM custody_holds')\n", encoding="utf-8")
            self.assertEqual(set(self.scan(Path(d))), {"sneaky_tool.py"})

    def test_the_payload_is_never_logged(self):
        src = (NORI_DIR / "custody.py").read_text(encoding="utf-8")
        for m in re.finditer(r"print\((.*)\)", src):
            self.assertNotIn("payload", m.group(1).replace("MAX_PAYLOAD", ""), m.group(0))


if __name__ == "__main__":
    unittest.main()

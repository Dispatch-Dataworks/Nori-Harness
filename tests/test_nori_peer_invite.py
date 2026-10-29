# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Connection strings (2026-10-29): connecting two agents used to need four values to line up across two pages (each side's inbound URL, which ends in
that side's own row id for the other and so cannot be known until the other row exists; a shared secret typed identically; the names). Now each agent makes
ONE string and each is pasted into the other.

Pinned: a pending invitation is not a peer (hidden from list_peers, disabled, nothing inbound reaches it); the string round-trips and survives being wrapped
by a chat app; every bad paste says what is wrong; pasting completes the same row in place; both sides arrive at the SAME secret without either "generating"
it, proved by a message signed by one side verifying on the other, in both directions; a string is single-purpose (own string refused, duplicate address
refused, discarded or expired invitations are gone); and the page shows the string, a copy button and the by-hand form under "advanced"."""
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_peer_invite_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_peer_invite_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"
for _f in ("persona.default.md", "persona.template.md"):
    shutil.copy(ROOT / "prompts" / _f, os.environ["NORI_PROMPTS_DIR"])

import accounts  # noqa: E402
import config  # noqa: E402
import crypto  # noqa: E402
import peers  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

store.init()
peers.renegotiate_now = lambda peer_id: None      # a completed connection introduces itself over the network; not in a unit test
_admin = accounts.bootstrap_admin("Ben", "testpass123")
SESS = {"user_id": _admin["id"], "workspace_id": _admin["workspace_id"], "role": "admin", "csrf": "csrf-tok"}
config.set("workspace", SESS["workspace_id"], "assistant_name", "Nori")


def _wipe():
    store.write(lambda c: c.execute("DELETE FROM peers"))


def _two_invites(a="https://nori.example.com", b="http://iron:8080"):
    """Two pending invitations, standing in for 'one on each agent' (same database, so same user; the logic is the same)."""
    ia = peers.create_invite(SESS, address=a)["peer_id"]
    ib = peers.create_invite(SESS, address=b)["peer_id"]
    return ia, ib


def _decode(s):
    body = s[len("paci1:"):]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


class Invitation(unittest.TestCase):
    def setUp(self):
        _wipe()

    def test_an_invitation_is_not_a_peer_yet(self):
        pid = peers.create_invite(SESS, address="https://nori.example.com")["peer_id"]
        self.assertEqual(peers.list_peers(SESS), [])
        self.assertEqual([i["id"] for i in peers.list_invites(SESS)], [pid])
        row = peers.get_peer(pid)
        self.assertEqual(row["enabled"], 0)
        # nothing the other side sends can land on it before it is completed
        code, resp = peers.handle_inbound(pid, method="POST", path=f"/paci/inbound/{pid}", headers={}, raw_body=b"{}")
        self.assertEqual(code, 404)
        self.assertIn("error", peers.set_peer_enabled(SESS, pid, True))
        self.assertEqual(peers.get_peer(pid)["enabled"], 0)

    def test_the_string_says_where_to_reach_this_agent(self):
        pid = peers.create_invite(SESS, address="https://nori.example.com/")["peer_id"]     # a trailing slash is tidied
        s = peers.connection_string(SESS, pid)
        self.assertTrue(s.startswith("paci1:"))
        d = _decode(s)
        self.assertEqual(d["v"], 1)
        self.assertEqual(d["name"], "Nori")
        self.assertEqual(d["url"], f"https://nori.example.com/paci/inbound/{pid}")        # this row's own id: the part nobody could know before
        self.assertEqual(d["key"], crypto.decrypt(peers.get_peer(pid)["invite_key_enc"]))
        self.assertEqual(peers.connection_string(SESS, pid), s, "stable: the same string every time it is shown")

    def test_the_string_is_url_safe_and_pastes_as_one_token(self):
        s = peers.connection_string(SESS, peers.create_invite(SESS, address="https://nori.example.com")["peer_id"])
        self.assertRegex(s, r"^paci1:[A-Za-z0-9_-]+$")

    def test_a_bad_address_is_refused(self):
        for bad in ("", "nori.example.com", "ftp://x.example.com", "https://user:pw@x.example.com", "https://x.example.com?a=1", "https://x .example.com", "javascript:alert(1)"):
            self.assertIn("error", peers.create_invite(SESS, address=bad), bad)
        self.assertEqual(peers.list_invites(SESS), [])

    def test_a_plain_http_internal_address_is_fine(self):
        d = _decode(peers.connection_string(SESS, peers.create_invite(SESS, address="http://iron:8080")["peer_id"]))
        self.assertTrue(d["url"].startswith("http://iron:8080/paci/inbound/"))

    def test_discarding_removes_it_and_only_an_invitation(self):
        pid = peers.create_invite(SESS, address="https://nori.example.com")["peer_id"]
        self.assertEqual(peers.discard_invite(SESS, pid), {"ok": True})
        self.assertIsNone(peers.get_peer(pid))
        real = peers.create_peer(SESS, scope="user", name="Real", url="https://x.example.com/paci/inbound/1", psk="k")["peer_id"]
        self.assertIn("error", peers.discard_invite(SESS, real), "a connected peer is deleted the ordinary way, never through here")
        self.assertIsNotNone(peers.get_peer(real))

    def test_an_unused_invitation_expires(self):
        pid = peers.create_invite(SESS, address="https://nori.example.com")["peer_id"]
        store.write(lambda c: c.execute("UPDATE peers SET created_ts=? WHERE id=?", (time.time() - 8 * 86400, pid)))
        self.assertEqual(peers.list_invites(SESS), [])
        self.assertIsNone(peers.get_peer(pid))

    def test_a_recent_one_is_kept(self):
        pid = peers.create_invite(SESS, address="https://nori.example.com")["peer_id"]
        store.write(lambda c: c.execute("UPDATE peers SET created_ts=? WHERE id=?", (time.time() - 6 * 86400, pid)))
        self.assertEqual(len(peers.list_invites(SESS)), 1)


class Parsing(unittest.TestCase):
    def setUp(self):
        _wipe()
        self.s = peers.connection_string(SESS, peers.create_invite(SESS, address="https://iron.example.com")["peer_id"])

    def test_a_good_string_parses(self):
        f, err = peers.parse_connection_string(self.s)
        self.assertEqual(err, "")
        self.assertEqual(f["name"], "Nori")
        self.assertTrue(f["url"].startswith("https://iron.example.com/paci/inbound/"))

    def test_wrapping_and_stray_whitespace_do_not_matter(self):
        wrapped = "\n  ".join(self.s[i:i + 20] for i in range(0, len(self.s), 20)) + "\n"
        f, err = peers.parse_connection_string("  " + wrapped + "  ")
        self.assertEqual(err, "")
        self.assertIsNotNone(f)

    def test_each_kind_of_wrong_paste_says_what_is_wrong(self):
        cases = {
            "": "not a connection string",
            "https://iron.example.com/paci/inbound/3": "not a connection string",
            "paci1:": "damaged",
            "paci1:!!!not-base64!!!": "damaged",
            self.s[:-12]: "damaged",
            "paci1:" + base64.urlsafe_b64encode(b'["a list"]').decode(): "damaged",
            "paci1:" + base64.urlsafe_b64encode(json.dumps({"v": 1, "name": "X", "url": "ftp://x", "key": "k" * 32}).encode()).decode(): "incomplete",
            "paci1:" + base64.urlsafe_b64encode(json.dumps({"v": 1, "name": "X", "url": "https://x.example.com/p/1", "key": "short"}).encode()).decode(): "incomplete",
            "paci1:" + base64.urlsafe_b64encode(json.dumps({"v": 9, "name": "X", "url": "https://x.example.com/p/1", "key": "k" * 32}).encode()).decode(): "newer version",
        }
        for text, expect in cases.items():
            f, err = peers.parse_connection_string(text)
            self.assertIsNone(f, text[:40])
            self.assertIn(expect, err, text[:40])


class Connecting(unittest.TestCase):
    def setUp(self):
        _wipe()

    def test_pasting_completes_the_same_row_in_place(self):
        ia, ib = _two_invites()
        sb = peers.connection_string(SESS, ib)
        r = peers.accept_connection_string(SESS, ia, sb, purpose="my other assistant")
        self.assertEqual(r["ok"], True)
        self.assertEqual(r["peer_id"], ia, "the row that was reserved is the connection")
        p = peers.get_peer(ia)
        self.assertEqual(p["enabled"], 1)
        self.assertEqual(p["name"], "Nori")                                    # their own name, from their string
        self.assertEqual(p["url"], _decode(sb)["url"])
        self.assertEqual(p["purpose"], "my other assistant")
        self.assertIsNone(p["invite_key_enc"])
        self.assertEqual([x["id"] for x in peers.list_peers(SESS)], [ia])
        self.assertEqual(peers.list_invites(SESS)[0]["id"], ib, "only the other, still-pending one is left")

    def test_a_name_can_be_chosen(self):
        ia, ib = _two_invites()
        r = peers.accept_connection_string(SESS, ia, peers.connection_string(SESS, ib), name="  Iron  ")
        self.assertEqual(r["name"], "Iron")
        self.assertEqual(peers.get_peer(ia)["name"], "Iron")

    def test_both_sides_arrive_at_the_same_secret_and_each_can_verify_the_other(self):
        # A and B are two agents; each pastes the other's string. Neither "generated" the secret.
        ia, ib = _two_invites(a="https://nori.example.com", b="https://iron.example.com")
        sa, sb = peers.connection_string(SESS, ia), peers.connection_string(SESS, ib)
        self.assertTrue(peers.accept_connection_string(SESS, ia, sb)["ok"])      # A learns B
        self.assertTrue(peers.accept_connection_string(SESS, ib, sa)["ok"])      # B learns A
        a, b = peers.get_peer(ia), peers.get_peer(ib)
        self.assertEqual(crypto.decrypt(a["psk_enc"]), crypto.decrypt(b["psk_enc"]))
        # the URLs cross over: A posts to where B receives, and B to where A receives
        self.assertEqual(a["url"], f"https://iron.example.com/paci/inbound/{ib}")
        self.assertEqual(b["url"], f"https://nori.example.com/paci/inbound/{ia}")
        # a message signed by A (as _post does it) is accepted by B's inbound check, and the reverse
        for sender, receiver in ((a, b), (b, a)):
            body = b'{"type":"health_check"}'
            ts, nonce = str(time.time()), "n-" + str(sender["id"]) + str(time.time())
            path = f"/paci/inbound/{receiver['id']}"
            sig = peers._sign(crypto.decrypt(sender["psk_enc"]), "POST", path, ts, nonce, body)
            hdrs = {"X-PACI-Timestamp": ts, "X-PACI-Nonce": nonce, "X-PACI-Signature": sig}
            self.assertIsNone(peers.verify_inbound(receiver, method="POST", path=path, headers=hdrs, body=body))
            code, resp = peers.handle_inbound(receiver["id"], method="POST", path=path, headers={**hdrs, "X-PACI-Nonce": nonce + "x",
                                              "X-PACI-Signature": peers._sign(crypto.decrypt(sender["psk_enc"]), "POST", path, ts, nonce + "x", body)}, raw_body=body)
            self.assertEqual((code, resp["type"]), (200, "health_check_ack"))

    def test_a_different_pairing_does_not_share_a_secret(self):
        ia, ib = _two_invites()
        ic = peers.create_invite(SESS, address="https://other.example.com")["peer_id"]
        peers.accept_connection_string(SESS, ia, peers.connection_string(SESS, ib))
        peers.accept_connection_string(SESS, ic, peers.connection_string(SESS, peers.create_invite(SESS, address="https://x.example.com")["peer_id"]))
        self.assertNotEqual(crypto.decrypt(peers.get_peer(ia)["psk_enc"]), crypto.decrypt(peers.get_peer(ic)["psk_enc"]))

    def test_the_agents_own_string_is_refused(self):
        ia, _ = _two_invites()
        r = peers.accept_connection_string(SESS, ia, peers.connection_string(SESS, ia))
        self.assertIn("own connection string", r["error"])
        self.assertEqual(peers.get_peer(ia)["enabled"], 0, "nothing changed")

    def test_the_same_address_twice_is_refused(self):
        ia, ib = _two_invites()
        sb = peers.connection_string(SESS, ib)
        self.assertTrue(peers.accept_connection_string(SESS, ia, sb)["ok"])
        ic = peers.create_invite(SESS, address="https://third.example.com")["peer_id"]
        r = peers.accept_connection_string(SESS, ic, sb)
        self.assertIn("Already connected", r["error"])
        self.assertEqual(peers.get_peer(ic)["enabled"], 0)

    def test_pasting_into_something_that_is_not_an_invitation_is_refused(self):
        real = peers.create_peer(SESS, scope="user", name="Real", url="https://x.example.com/paci/inbound/1", psk="k")["peer_id"]
        _, ib = _two_invites()
        r = peers.accept_connection_string(SESS, real, peers.connection_string(SESS, ib))
        self.assertIn("error", r)
        self.assertEqual(crypto.decrypt(peers.get_peer(real)["psk_enc"]), "k", "a connected peer's secret is never replaced this way")
        self.assertIn("error", peers.accept_connection_string(SESS, 99999, peers.connection_string(SESS, ib)))

    def test_a_bad_paste_leaves_the_invitation_waiting(self):
        ia, _ = _two_invites()
        self.assertIn("error", peers.accept_connection_string(SESS, ia, "paci1:nonsense"))
        self.assertIn("error", peers.accept_connection_string(SESS, ia, "garbage"))
        self.assertEqual(peers.get_peer(ia)["enabled"], 0)
        self.assertIn(ia, [i["id"] for i in peers.list_invites(SESS)])

    def test_the_whole_household_scope_is_honoured(self):
        ia, ib = _two_invites()
        peers.accept_connection_string(SESS, ia, peers.connection_string(SESS, ib), scope="workspace")
        p = peers.get_peer(ia)
        self.assertEqual((p["scope"], p["scope_id"]), ("workspace", SESS["workspace_id"]))
        ic = peers.create_invite(SESS, address="https://third.example.com")["peer_id"]
        bad = peers.accept_connection_string(SESS, ib, peers.connection_string(SESS, ic), scope="everyone")
        self.assertIn("scope must be one of", bad["error"])
        self.assertEqual(peers.get_peer(ib)["enabled"], 0, "nothing was half-done")

    def test_a_completed_connection_gets_its_tools_and_a_pending_one_has_none(self):
        import tools
        ia, ib = _two_invites()
        self.assertNotIn(f"peer{ia}_send", tools._REGISTRY)
        peers.accept_connection_string(SESS, ia, peers.connection_string(SESS, ib))
        self.assertIn(f"peer{ia}_send", tools._REGISTRY)
        self.assertIn(f"peer{ia}_check", tools._REGISTRY)
        self.assertNotIn(f"peer{ib}_send", tools._REGISTRY)


class _H(server.Handler):
    def __init__(self):
        self.headers = {"Host": "nori.example.com"}
        self.raw = []

    def _settings_response(self, sess, tab, body, **kw):
        self.raw.append(body)

    def send(self, *a, **kw):
        self.raw.append(a)


class Page(unittest.TestCase):
    def setUp(self):
        _wipe()

    def _page(self, **kw):
        h = _H()
        h.peers_admin_form(dict(SESS), **kw)
        return h.raw[-1]

    def test_the_page_leads_with_connection_strings(self):
        html = self._page()
        self.assertIn("connect to another agent", html)
        self.assertIn("make my connection string", html)
        self.assertIn("value='https://nori.example.com'", html, "the address defaults to this host")
        self.assertLess(html.index("connect to another agent"), html.index("connect by hand"))
        self.assertIn("<details class=sched-history><summary>connect by hand", html, "the old form is still there, tucked away")
        self.assertIn("name=psk", html)

    def test_a_pending_invitation_shows_its_string_a_copy_button_and_the_paste_box(self):
        pid = peers.create_invite(SESS, address="https://nori.example.com")["peer_id"]
        html = self._page()
        self.assertIn(peers.connection_string(SESS, pid), html)
        self.assertIn(f"data-copy-from='inv-{pid}'", html)
        self.assertIn(f"id='inv-{pid}'", html)
        self.assertIn(f"/admin/peers/invite/{pid}/accept", html)
        self.assertIn(f"/admin/peers/invite/{pid}/discard", html)
        self.assertIn("name=string", html)
        self.assertNotIn("(waiting for the other agent)", html, "a pending row is not listed as a peer")

    def test_the_copy_button_has_a_handler_that_needs_no_inline_script(self):
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn("[data-copy-from]", src)
        self.assertNotIn("onclick=", self._page_with_invite())

    def _page_with_invite(self):
        peers.create_invite(SESS, address="https://nori.example.com")
        return self._page()

    def test_making_one_through_the_handler_shows_it(self):
        h = _H()
        h.peers_invite_post(dict(SESS), {"address": "http://iron:8080"})
        self.assertEqual(len(peers.list_invites(SESS)), 1)
        self.assertIn("paci1:", h.raw[-1])
        h.peers_invite_post(dict(SESS), {"address": "not a url"})
        self.assertIn("the address must look like", h.raw[-1])
        self.assertEqual(len(peers.list_invites(SESS)), 1)

    def test_accepting_through_the_handler_connects(self):
        ia, ib = _two_invites()
        h = _H()
        h.peers_invite_action_post(dict(SESS), str(ia), "accept", {"string": peers.connection_string(SESS, ib), "name": "", "purpose": "", "scope": "user"})
        self.assertIn("connected to Nori", h.raw[-1])
        self.assertEqual(peers.get_peer(ia)["enabled"], 1)
        h.peers_invite_action_post(dict(SESS), str(ib), "accept", {"string": "wrong", "scope": "user"})
        self.assertIn("not a connection string", h.raw[-1])
        h.peers_invite_action_post(dict(SESS), str(ib), "discard", {})
        self.assertIsNone(peers.get_peer(ib))
        h.peers_invite_action_post(dict(SESS), "abc", "accept", {})
        self.assertIn("bad id", h.raw[-1])

    def test_only_an_admin_gets_any_of_it(self):
        h = _H()
        h.forbidden = lambda: h.raw.append("forbidden")
        member = dict(SESS, role="member")
        h.peers_invite_post(member, {"address": "https://x.example.com"})
        h.peers_invite_action_post(member, "1", "accept", {})
        self.assertEqual(h.raw, ["forbidden", "forbidden"])
        self.assertEqual(peers.list_invites(SESS), [])


if __name__ == "__main__":
    unittest.main()

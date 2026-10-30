# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""History pruning (2026-10-20): search the chat history on the History page, then
HIDE the picked messages from her context (reversible) or DELETE them for good.

What matters, and is tested here against the real SQLite store and real functions:
  * a hidden message reaches NOTHING the model is shown -- not her recent context,
    not compaction summaries (and a stored summary that touched it is dropped), not
    her search_history tool, not the reflection window;
  * the human views still show hidden messages (that is how they're found again);
  * delete really deletes, including an image message's stored file, and only that
    user's rows; the audit trail never holds message text;
  * the edit route is behind the session + CSRF gates, scopes every id to the user,
    recomputes "apply to all" server-side, and redirects only to a sanitized /history;
  * a structural guard: every function in conversation.py that reads `messages` is
    classified as model-facing (and filters hidden rows) or human/bookkeeping.
"""
import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_histprune_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_histprune_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import compaction  # noqa: E402
import context  # noqa: E402
import conversation  # noqa: E402
import imagegen  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Tester", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
_uid2, _tok2 = accounts.create_invite(WS, "Second", "member", UID)
_member = accounts.accept_invite(_tok2, "memberpass123")
UID2 = _member["id"]
SESS = {"user_id": UID, "workspace_id": WS, "role": "admin", "csrf": "csrf-tok"}
BASE_TS = time.time() - 10 * 86400


def _read(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


def _reset():
    for tbl in ("messages", "conversation_segments", "history_edits"):
        store.write(lambda c, t=tbl: c.execute(f"DELETE FROM {t}"))


def _add(user_id, role, content, *, minutes=0, kind="chat", meta=None, ts=None) -> int:
    mid = conversation.add_message(user_id, role, content, kind=kind, meta=meta)
    store.write(lambda c: c.execute("UPDATE messages SET ts=? WHERE id=?",
                                    (ts if ts is not None else BASE_TS + minutes * 60, mid)))
    return mid


def _flag(mid: int) -> int:
    return store.read(lambda c: c.execute("SELECT COALESCE(pruned,0) p FROM messages WHERE id=?", (mid,)).fetchone())["p"]


def _exists(mid: int) -> bool:
    return store.read(lambda c: c.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone()) is not None


def _texts(rows):
    return [r["content"] for r in rows]


class ModelFacingReadersNeverSeeHiddenMessages(unittest.TestCase):
    def setUp(self):
        _reset()
        self.a = _add(UID, "user", "my favorite color is teal", minutes=0)
        self.b = _add(UID, "assistant", "teal is lovely", minutes=1)
        self.c = _add(UID, "user", "unrelated chatter about the weather", minutes=2)
        conversation.set_pruned(UID, [self.a, self.b], pruned=True)

    def test_recent_excludes_hidden_by_default_and_includes_them_for_display(self):
        self.assertEqual(_texts(conversation.recent(UID)), ["unrelated chatter about the weather"])
        self.assertEqual(len(conversation.recent(UID, include_pruned=True)), 3)
        self.assertEqual(len(conversation.recent(UID, include_tool=True)), 1, "include_tool alone doesn't expose hidden")

    def test_restore_brings_them_back(self):
        conversation.set_pruned(UID, [self.a], pruned=False)
        self.assertEqual(_texts(conversation.recent(UID)), ["my favorite color is teal",
                                                            "unrelated chatter about the weather"])

    def test_compaction_inputs_exclude_hidden(self):
        self.assertEqual(_texts(conversation.before(UID, self.c + 1)), ["unrelated chatter about the weather"])
        self.assertEqual(_texts(conversation.in_range(UID, self.a, self.c)), ["unrelated chatter about the weather"])

    def test_a_hidden_unanswered_user_message_is_not_something_to_answer(self):
        self.assertIsNone(conversation.latest_user_message_after(UID, self.c))
        d = _add(UID, "user", "a real unanswered one", minutes=3)
        conversation.set_pruned(UID, [d], pruned=True)
        self.assertEqual(conversation.latest_user_message_after(UID, self.b)["id"], self.c)

    def test_her_search_history_tool_never_finds_or_shows_hidden(self):
        r = conversation.search(UID, query="teal")
        self.assertEqual((r["segments"], r["match_count"]), ([], 0))
        # hidden neighbours aren't pulled into a segment around a visible match either
        v = _add(UID, "user", "visible teal mention", minutes=4)
        for i in range(30):                       # push it outside the live window so it can match
            _add(UID, "user", f"filler {i}", minutes=5 + i)
        r = conversation.search(UID, query="visible teal")
        blob = json.dumps(r)
        self.assertIn("visible teal mention", blob)
        self.assertNotIn("my favorite color", blob)
        self.assertNotIn("teal is lovely", blob)
        self.assertEqual(v, v)

    def test_context_build_messages_never_contains_hidden_text(self):
        msgs = context.build_messages(UID, "Tester")
        blob = json.dumps(msgs)
        self.assertNotIn("favorite color is teal", blob)
        self.assertNotIn("teal is lovely", blob)
        self.assertIn("weather", blob)
        conversation.set_pruned(UID, [self.a], pruned=False)
        self.assertIn("favorite color is teal", json.dumps(context.build_messages(UID, "Tester")))

    def test_the_composition_breakdown_counts_only_what_she_sees(self):
        window_tokens = lambda: next(r for r in context.composition_breakdown(  # noqa: E731
            {"user_id": UID, "workspace_id": WS, "role": "admin"}, UID, "Tester")["sections"]
            if r["label"] == "raw conversation window")["chars"]
        hidden_chars = window_tokens()
        conversation.set_pruned(UID, [self.a, self.b], pruned=False)
        self.assertGreater(window_tokens(), hidden_chars)

    def test_compaction_summarizes_only_visible_messages_and_drops_stale_summaries(self):
        _reset()
        ids = [_add(UID, "user" if i % 2 == 0 else "assistant", f"line {i} secret-{i}" if i == 2 else f"line {i}",
                    minutes=i) for i in range(6)]
        # an older session, so it is eligible for a summary
        later = _add(UID, "user", "much later", ts=BASE_TS + 5 * 86400)
        seen = []

        def fake_call(messages, **kw):
            seen.append(messages[-1]["content"])
            return {"content": "A summary."}
        with patch.object(compaction.chat, "call", fake_call):
            compaction.compact_user(UID, "Tester")
        self.assertTrue(any("secret-2" in t for t in seen), "visible at first: it is summarized")
        self.assertEqual(store.read(lambda c: c.execute("SELECT COUNT(*) n FROM conversation_segments").fetchone())["n"], 1)
        # hide it -> the summary that contained it is dropped and rebuilt WITHOUT it
        changed = conversation.set_pruned(UID, [ids[2]], pruned=True)
        self.assertEqual(compaction.invalidate_around(UID, changed), 1)
        seen.clear()
        with patch.object(compaction.chat, "call", fake_call):
            compaction.compact_user(UID, "Tester")
        self.assertTrue(seen)
        self.assertFalse(any("secret-2" in t for t in seen), "a hidden message is never fed to the summarizer")
        self.assertEqual(later, later)


class HumanViewsStillSeeEverything(unittest.TestCase):
    def setUp(self):
        _reset()
        self.a = _add(UID, "user", "alpha thing", minutes=0)
        self.b = _add(UID, "assistant", "beta thing", minutes=1)
        self.c = _add(UID, "user", "gamma", minutes=2)
        conversation.set_pruned(UID, [self.b], pruned=True)

    def test_history_page_and_search_include_hidden(self):
        self.assertEqual({m["id"] for m in conversation.history_page(UID)}, {self.a, self.b, self.c})
        self.assertEqual(conversation.history_match_ids(UID, query="thing"), [self.b, self.a])

    def test_only_pruned_filters_both(self):
        self.assertEqual([m["id"] for m in conversation.history_page(UID, only_pruned=True)], [self.b])
        self.assertEqual(conversation.history_match_ids(UID, query="thing", only_pruned=True), [self.b])
        self.assertEqual(conversation.history_match_ids(UID, only_pruned=True), [self.b], "no query needed for hidden-only")
        self.assertEqual(conversation.history_match_ids(UID), [], "still nothing without any scope")
        self.assertEqual(conversation.count_hidden(UID), 1)

    def test_since_feed_used_for_display_still_includes_hidden(self):
        self.assertIn(self.b, [m["id"] for m in conversation.since(UID, 0)])


class SetPrunedAndDelete(unittest.TestCase):
    def setUp(self):
        _reset()
        self.mine = [_add(UID, "user", f"mine {i}", minutes=i) for i in range(4)]
        self.theirs = _add(UID2, "user", "someone else's", minutes=0)

    def test_set_pruned_returns_only_what_changed_and_is_user_scoped(self):
        self.assertEqual(conversation.set_pruned(UID, self.mine[:2] + [self.theirs, 999999, "x", -3, None], pruned=True),
                         self.mine[:2])
        self.assertEqual(_flag(self.theirs), 0, "another account's message is untouchable")
        self.assertEqual(conversation.set_pruned(UID, self.mine[:2], pruned=True), [], "already hidden: no change")
        self.assertEqual(conversation.set_pruned(UID, self.mine[:1], pruned=False), self.mine[:1])
        self.assertEqual(conversation.set_pruned(UID, [], pruned=True), [])

    def test_delete_removes_rows_only_for_that_user(self):
        res = conversation.delete_messages(UID, self.mine[:2] + [self.theirs, 424242])
        self.assertEqual(res["ids"], self.mine[:2])
        self.assertFalse(_exists(self.mine[0]) or _exists(self.mine[1]))
        self.assertTrue(_exists(self.theirs) and _exists(self.mine[2]))
        self.assertEqual(conversation.delete_messages(UID, [self.theirs]), {"ids": [], "files": 0})
        self.assertEqual(conversation.delete_messages(UID, []), {"ids": [], "files": 0})

    def test_delete_removes_an_image_messages_stored_file_and_thumbnail(self):
        fid = "ab" * 16
        (imagegen.GEN_DIR / fid).write_bytes(b"\xff\xd8fake")
        imagegen.thumb_path(fid).write_bytes(b"thumb")
        img = _add(UID, "user", "", kind="image", meta={"file_id": fid, "mime": "image/jpeg"})
        res = conversation.delete_messages(UID, [img])
        self.assertEqual((res["ids"], res["files"]), ([img], 2))
        self.assertFalse((imagegen.GEN_DIR / fid).exists() or imagegen.thumb_path(fid).exists())

    def test_a_file_another_message_still_uses_is_kept(self):
        fid = "cd" * 16
        (imagegen.GEN_DIR / fid).write_bytes(b"x")
        one = _add(UID, "user", "", kind="image", meta={"file_id": fid})
        two = _add(UID, "assistant", "", kind="image", meta={"file_id": fid})
        self.assertEqual(conversation.delete_messages(UID, [one])["files"], 0)
        self.assertTrue((imagegen.GEN_DIR / fid).exists())
        self.assertEqual(conversation.delete_messages(UID, [two])["files"], 1)
        self.assertFalse((imagegen.GEN_DIR / fid).exists())

    def test_a_hostile_file_id_never_becomes_a_path(self):
        victim = imagegen.GEN_DIR.parent / "victim.txt"
        victim.write_text("keep me")
        for evil in ("../victim.txt", "..\\victim.txt", "/etc/passwd", "a" * 200, "", None, 5, ["x"]):
            img = _add(UID, "user", "", kind="image", meta={"file_id": evil})
            conversation.delete_messages(UID, [img])
        self.assertEqual(victim.read_text(), "keep me")

    def test_the_audit_row_holds_counts_and_ids_never_text(self):
        conversation.record_edit(UID, "delete", [5, 9, 7], query="the secret phrase", note="1 picture file(s) removed")
        row = dict(store.read(lambda c: c.execute("SELECT * FROM history_edits").fetchone()))
        self.assertEqual((row["action"], row["message_count"], row["first_id"], row["last_id"]), ("delete", 3, 5, 9))
        conversation.record_edit(UID, "hide", [])
        self.assertEqual(store.read(lambda c: c.execute("SELECT COUNT(*) n FROM history_edits").fetchone())["n"], 1)
        self.assertNotIn("content", row)


class SummariesAreInvalidatedAroundAChange(unittest.TestCase):
    def setUp(self):
        _reset()

    def _seg(self, user_id, lo, hi):
        store.write(lambda c: c.execute(
            "INSERT INTO conversation_segments(user_id, lo_id, hi_id, lo_ts, hi_ts, text, generated_ts) "
            "VALUES (?,?,?,?,?,?,?)", (user_id, lo, hi, 0, 1, f"summary {lo}-{hi}", 0)))

    def _left(self, user_id=UID):
        return [(r["lo_id"], r["hi_id"]) for r in store.read(lambda c: c.execute(
            "SELECT lo_id, hi_id FROM conversation_segments WHERE user_id=? ORDER BY lo_id", (user_id,)).fetchall())]

    def test_the_containing_segment_and_both_neighbours_go_the_rest_stay(self):
        for lo, hi in ((1, 9), (10, 19), (20, 29), (30, 39), (40, 49)):
            self._seg(UID, lo, hi)
        self.assertEqual(compaction.invalidate_around(UID, [25]), 3)
        self.assertEqual(self._left(), [(1, 9), (40, 49)])

    def test_a_message_in_the_gap_between_segments_still_drops_both_sides(self):
        for lo, hi in ((1, 9), (20, 29), (40, 49)):
            self._seg(UID, lo, hi)
        self.assertEqual(compaction.invalidate_around(UID, [15]), 2)        # 15 is inside no segment
        self.assertEqual(self._left(), [(40, 49)])

    def test_edges_and_nothing_to_do(self):
        self._seg(UID, 1, 9)
        self.assertEqual(compaction.invalidate_around(UID, [500]), 1, "the nearest segment before it")
        self.assertEqual(compaction.invalidate_around(UID, []), 0)
        self.assertEqual(compaction.invalidate_around(UID, ["x", -1]), 0)

    def test_another_users_summaries_are_never_touched(self):
        self._seg(UID, 1, 9)
        self._seg(UID2, 1, 9)
        compaction.invalidate_around(UID, [5])
        self.assertEqual(self._left(UID2), [(1, 9)])

    def test_works_for_ids_that_no_longer_exist_after_a_delete(self):
        self._seg(UID, 1, 9)
        self.assertEqual(compaction.invalidate_around(UID, [5]), 1)


class _H(server.Handler):
    def __init__(self, body: bytes = b"", multi=None):
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.sent = []
        self._form_multi = multi or {}
        self.command = "POST"

    def send(self, code, body=b"", extra=None, *, ctype="text/html; charset=utf-8"):
        self.sent.append({"code": code, "body": body, "extra": extra or {}, "ctype": ctype})

    def location(self):
        r = self.sent[-1]
        return r["code"], r["extra"].get("Location")

    def html(self):
        return self.sent[-1]["body"].decode("utf-8", "replace")


def _edit(action="hide", mids=(), *, sess=SESS, **form):
    h = _H(multi={"mid": [str(m) for m in mids]})
    h.history_edit_post(sess, {"action": action, **form})
    return h


class EditRoute(unittest.TestCase):
    def setUp(self):
        _reset()
        self.ids = [_add(UID, "user" if i % 2 == 0 else "assistant", f"turnip talk {i}", minutes=i) for i in range(6)]
        self.other = _add(UID2, "user", "turnip from someone else", minutes=0)

    def test_hide_restore_delete_the_ticked_rows(self):
        code, loc = _edit("hide", self.ids[:2]).location()
        self.assertEqual((code, loc), (303, "/history?done=hide&n=2"))
        self.assertEqual([_flag(i) for i in self.ids[:3]], [1, 1, 0])
        code, loc = _edit("restore", self.ids[:1]).location()
        self.assertEqual(loc, "/history?done=restore&n=1")
        self.assertEqual(_flag(self.ids[0]), 0)
        code, loc = _edit("delete", self.ids[4:6]).location()
        self.assertEqual(loc, "/history?done=delete&n=2")
        self.assertFalse(_exists(self.ids[4]) or _exists(self.ids[5]))

    def test_every_edit_is_recorded_without_text(self):
        _edit("hide", self.ids[:2], q="turnip")
        _edit("delete", self.ids[2:3])
        rows = [dict(r) for r in store.read(lambda c: c.execute("SELECT * FROM history_edits ORDER BY id").fetchall())]
        self.assertEqual([(r["action"], r["message_count"]) for r in rows], [("hide", 2), ("delete", 1)])
        self.assertEqual(rows[0]["query"], "turnip")
        self.assertNotIn("turnip talk", json.dumps(rows))

    def test_apply_to_all_is_recomputed_on_the_server_from_the_search_not_the_page(self):
        # the page only ticked one row, but all_matches asks for everything the search matches
        _edit("hide", [self.ids[0]], all_matches="1", q="turnip talk")
        self.assertEqual([_flag(i) for i in self.ids], [1] * 6)
        self.assertEqual(_flag(self.other), 0, "never another user's messages, even though they match")

    def test_apply_to_all_without_any_scope_is_refused(self):
        code, loc = _edit("delete", [self.ids[0]], all_matches="1").location()
        self.assertEqual(loc, "/history?done=nosearch")
        self.assertTrue(all(_exists(i) for i in self.ids))

    def test_apply_to_all_with_the_hidden_filter_and_no_text(self):
        conversation.set_pruned(UID, self.ids[:3], pruned=True)
        _edit("restore", [], all_matches="1", hidden="1")
        self.assertEqual([_flag(i) for i in self.ids], [0] * 6)

    def test_apply_to_all_refuses_an_unreasonably_large_scope(self):
        with patch.object(conversation, "MAX_EDIT_IDS", 3):
            code, loc = _edit("delete", [], all_matches="1", q="turnip").location()
        self.assertEqual(loc, "/history?done=toomany")
        self.assertTrue(all(_exists(i) for i in self.ids))

    def test_nothing_selected_bad_action_and_no_ops(self):
        self.assertEqual(_edit("hide", []).location()[1], "/history?done=none")
        self.assertEqual(_edit("explode", self.ids).location()[1], "/history?done=none")
        self.assertEqual(_edit("", self.ids).location()[1], "/history?done=none")
        self.assertTrue(all(_flag(i) == 0 for i in self.ids))
        _edit("hide", self.ids[:1])
        self.assertEqual(_edit("hide", self.ids[:1]).location()[1], "/history?done=nochange", "already hidden")
        self.assertEqual(_edit("delete", [987654]).location()[1], "/history?done=nochange")

    def test_ids_are_scoped_to_the_signed_in_user(self):
        _edit("delete", [self.other] + self.ids[:1])
        self.assertTrue(_exists(self.other))
        self.assertFalse(_exists(self.ids[0]))
        member = {**SESS, "user_id": UID2, "role": "member"}
        _edit("hide", self.ids[1:3], sess=member)
        self.assertEqual([_flag(i) for i in self.ids[1:3]], [0, 0], "a member can't touch the admin's history")
        _edit("hide", [self.other], sess=member)
        self.assertEqual(_flag(self.other), 1, "but members can edit their own")

    def test_return_query_is_sanitized_to_known_params_on_history(self):
        h = _edit("hide", self.ids[:1], return_q="q=turnip&hours_ago=5&hidden=1&jump=3&evil=1&done=delete&n=99"
                                                 "&url=http://evil.example/")
        code, loc = h.location()
        self.assertTrue(loc.startswith("/history?"))
        self.assertNotIn("evil", loc)
        self.assertNotIn("http", loc)
        self.assertEqual(loc.count("done="), 1, "the old flash isn't carried over")
        self.assertIn("q=turnip", loc)
        for bad in ("//evil.example", "https://evil.example", "/../x", "%0d%0aSet-Cookie: x=1"):
            loc = _edit("hide", self.ids[1:2], return_q=bad).location()[1]
            self.assertTrue(loc.startswith("/history"), (bad, loc))
            self.assertNotIn("\n", loc)

    def test_jump_is_dropped_after_a_delete_since_its_target_may_be_gone(self):
        loc = _edit("delete", self.ids[:1], return_q="q=turnip&jump=%d" % self.ids[0]).location()[1]
        self.assertNotIn("jump=", loc)
        self.assertIn("q=turnip", loc)

    def test_deleting_drops_the_summaries_around_it(self):
        store.write(lambda c: c.execute(
            "INSERT INTO conversation_segments(user_id, lo_id, hi_id, lo_ts, hi_ts, text, generated_ts) VALUES (?,?,?,?,?,?,?)",
            (UID, self.ids[0], self.ids[-1], 0, 1, "a summary containing turnip talk", 0)))
        _edit("delete", [self.ids[2]])
        self.assertEqual(store.read(lambda c: c.execute("SELECT COUNT(*) n FROM conversation_segments").fetchone())["n"], 0)

    def test_the_route_is_behind_the_session_and_csrf_gates(self):
        import inspect
        src = inspect.getsource(server.Handler._do_POST_inner)
        route = src.index('"/history/edit"')
        self.assertGreater(route, src.index("self.csrf_ok(sess, form)"))
        self.assertGreater(route, src.index("form = self.form()"))

    def test_form_keeps_every_value_of_a_repeated_key(self):
        body = b"csrf=x&action=hide&mid=1&mid=2&mid=3"
        h = _H(body)
        out = h.form()
        self.assertEqual(out["action"], "hide")
        self.assertEqual(h._form_multi["mid"], ["1", "2", "3"])


class PageRendering(unittest.TestCase):
    def setUp(self):
        _reset()
        self.ids = [_add(UID, "user" if i % 2 == 0 else "assistant", f"rutabaga note {i}", minutes=i) for i in range(5)]
        conversation.set_pruned(UID, [self.ids[1]], pruned=True)

    def _page(self, **q):
        h = _H()
        h.history_page(SESS, {k: [v] for k, v in q.items()})
        return h.html()

    def test_every_row_has_a_checkbox_and_hidden_ones_are_marked(self):
        html = self._page()
        self.assertEqual(html.count("name=mid value="), 5)
        self.assertIn("hidden from ⟦n⟧", html)
        self.assertEqual(html.count("hist-row selectable user is-hidden") + html.count("hist-row selectable assistant is-hidden"), 1,
                         "exactly the one hidden row is dimmed")
        self.assertIn("hist-badge-hidden", html)

    def test_the_toolbar_has_the_three_actions_and_a_real_delete_confirmation(self):
        html = self._page()
        for needle in ("value=hide", "value=restore", "value=delete", "Hide from ⟦n⟧", "Restore", "Delete forever",
                       "Permanently delete {n} message(s)", "backups made earlier", "id=histSelCount", "id=histSelAll"):
            self.assertIn(needle, html)
        self.assertIn("name=csrf value='csrf-tok'", html)
        self.assertIn("action='/history/edit'", html)

    def test_searching_offers_apply_to_all_with_the_real_count(self):
        html = self._page(q="rutabaga")
        self.assertIn("Apply to all 5 message(s) that match this search", html)
        self.assertIn("data-count='5'", html)
        self.assertNotIn("id=histAll", self._page(), "no apply-to-all without a search or the hidden filter")

    def test_hidden_only_filter_lists_just_those_and_counts_them(self):
        html = self._page(hidden="1")
        self.assertEqual(html.count("name=mid value="), 1)
        self.assertIn("rutabaga note 1", html)
        self.assertNotIn("rutabaga note 0", html)
        self.assertIn("Apply to all 1 message(s) that are hidden from ⟦n⟧", html)
        self.assertIn("hidden from ⟦n⟧ only (1)", html)
        self.assertIn("value=1 checked", html.split("<label class=hist-only>")[1][:120])

    def test_hidden_only_with_nothing_hidden_says_so(self):
        conversation.set_pruned(UID, self.ids, pruned=False)
        self.assertIn("nothing is hidden from ⟦n⟧.", self._page(hidden="1"))

    def test_flash_messages_come_from_an_allowlist_not_the_url(self):
        self.assertIn("Hid 3 messages from ⟦n⟧&#x27;s context.", self._page(done="hide", n="3"))
        self.assertIn("Hid 1 message from ⟦n⟧&#x27;s context.", self._page(done="hide", n="1"))
        self.assertIn("Permanently deleted 2 messages and 1 stored picture file(s).", self._page(done="delete", n="2", f="1"))
        self.assertIn("Nothing was selected.", self._page(done="none"))
        evil = self._page(done="<script>alert(1)</script>", n="1")
        self.assertNotIn("<script>alert(1)", evil)
        self.assertEqual(self._page(done="hide", n="abc").count("class=info"), 0)

    def test_the_search_text_is_escaped_everywhere_it_appears(self):
        html = self._page(q="'><script>alert(1)</script>")
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_the_explanation_mentions_memory_and_what_each_choice_does(self):
        html = self._page()
        self.assertIn("never sees it again", html)
        self.assertIn("including a photo", html)
        self.assertIn("/settings?tab=memory", html)

    def test_a_search_still_centres_on_the_match_across_hidden_messages(self):
        html = self._page(q="note 1")
        self.assertIn("hist-hit", html)
        self.assertIn("match 1 of", html)

    def test_chat_page_still_displays_hidden_messages_with_a_marker(self):
        src = _read(os.path.join(ROOT, "server.py"))
        self.assertIn("conversation.recent(sess[\"user_id\"], include_tool=show_tools, include_pruned=True)", src)
        self.assertIn("msg-hidden", src)
        self.assertIn("Hidden from ⟦n⟧", src)

    def test_the_edit_script_is_its_own_script_element_and_parses(self):
        js = server.HISTORY_EDIT_JS
        self.assertTrue(js.startswith("<script>") and js.endswith("</script>"))
        self.assertEqual(js.count("<script>"), 1)
        self.assertNotIn("innerHTML", js)
        body = js[len("<script>"):-len("</script>")]
        try:
            node = subprocess.run(["node", "--version"], capture_output=True)
        except FileNotFoundError:
            self.skipTest("node isn't available")
        if node.returncode != 0:
            self.skipTest("node isn't available")
        path = os.path.join(os.environ["NORI_DATA_DIR"], "hist_edit.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("HISTORY_EDIT_JS", _read(os.path.join(ROOT, "server.py")))
        self.assertIn("extra_js=HISTORY_JUMP_JS + HISTORY_EDIT_JS", _read(os.path.join(ROOT, "server.py")))


class EveryReaderOfMessagesIsClassified(unittest.TestCase):
    """The leak guard. conversation.py is the only module that reads `messages` for
    the model; each function that does must be either model-facing (and filter hidden
    rows: the pruned clause is in its SQL) or listed here as human/bookkeeping with
    the reason. A NEW reader fails this test until someone decides which it is."""

    MODEL_FACING = {"recent", "latest_user_message_after", "before", "in_range", "search"}
    NOT_MODEL_FACING = {
        "attach_cost_since": "writes cost meta onto peer_proactive rows",
        "last_message_ts": "a timestamp for pacing, no content",
        "last_user_message_ts": "a timestamp for pacing, no content",
        "count": "a number for the UI",
        "count_since_ts": "a number: reflection cadence",
        "max_id": "bookkeeping", "min_id": "bookkeeping",
        "since": "the chat page's own poll feed -- his display",
        "get_own": "/retry ownership check",
        "has_reply_after": "/retry guard",
        "history_page": "the History page: a human view that must show hidden rows",
        "history_match_ids": "the History page's search: must find hidden rows to restore/delete them",
        "set_pruned": "the edit operation itself",
        "delete_messages": "the edit operation itself",
        "count_hidden": "a number for the UI",
        "assistant_messages_after": "notifications for the owner's own devices, not a prompt; still excludes hidden messages",
        "count_assistant_chat_since": "a number: onboarding ends itself after too many of her replies; no text is read",
        "delete_all_for_workspace": "delete only (start over, firstrun.reset); no text is read",
        "workspace_has_messages": "a yes/no: first-run setup is skipped for an instance that already has history; no text is read",
        "add_message": "insert",
        "cost_summary": "cost reporting",
    }

    def _functions_reading_messages(self):
        src = _read(os.path.join(ROOT, "conversation.py"))
        tree = ast.parse(src)
        out = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                seg = ast.get_source_segment(src, node) or ""
                if "FROM messages" in seg or "INTO messages" in seg or "UPDATE messages" in seg:
                    out[node.name] = seg
        return out

    def test_each_function_is_classified(self):
        funcs = self._functions_reading_messages()
        unclassified = sorted(set(funcs) - self.MODEL_FACING - set(self.NOT_MODEL_FACING))
        self.assertEqual(unclassified, [], "these functions read `messages` but aren't classified: decide whether they feed "
                                           "the model (then they must exclude pruned rows) or not (then say why)")

    def test_model_facing_readers_filter_hidden_rows(self):
        funcs = self._functions_reading_messages()
        for name in self.MODEL_FACING:
            seg = funcs[name]
            self.assertTrue("COALESCE(pruned,0)=0" in seg or "_VISIBLE_TO_HER" in seg, name)

    def test_no_other_module_reads_messages_for_a_prompt(self):
        allowed = {"conversation.py": "see above", "store.py": "schema/migrations", "diagnostics.py": "kind='tool' meta",
                   "debug_api.py": "the debug endpoint (read-only, scrubbed, token-gated): shows a developer messages including pruned ones; never a model",
                   "bridge.py": "updates one message's meta", "peers.py": "an existence check on user messages"}
        for fname in sorted(os.listdir(ROOT)):
            if not fname.endswith(".py"):
                continue
            text = _read(os.path.join(ROOT, fname))
            if "FROM messages" in text or "UPDATE messages" in text or "DELETE FROM messages" in text:
                self.assertIn(fname, allowed, f"{fname} reads or writes `messages` directly -- route it through conversation.py")


if __name__ == "__main__":
    unittest.main()

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Byte-identical proof for the memory backend seam (2026-09-25, the operator:
"pointing an existing mechanism at an external store... local is the
reference implementation and the contract"). This file's own assertions
were written by running the CURRENT (pre-refactor) code and recording
its real output as the expected value -- the same discipline the
assistant-rename feature's own default-invariance test used -- so a
later run of this SAME file against the refactored code is the proof
that local's behaviour didn't move at all: a pure seam extraction, never
a rewrite. If this file ever needs an assertion changed to pass, that is
itself the signal that the refactor changed behaviour, not just moved it.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_memory_seam_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_memory_seam_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import config  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402

store.init()


def _fake_embed(vectors_by_text):
    def _call(texts, *, model):
        out = []
        for t in texts:
            out.append(vectors_by_text.get(t, [0.0, 0.0, 1.0]))
        return {"ok": True, "vectors": out}
    return _call


class MemoryBackendSeamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.user = accounts.bootstrap_admin("Tester", "testpass123")
        cls.uid = cls.user["id"]
        cls.wsid = cls.user["workspace_id"]
        cls.session = {"user_id": cls.uid, "workspace_id": cls.wsid, "role": "admin"}

    def setUp(self):
        store.write(lambda c: c.execute("DELETE FROM memory WHERE user_id=?", (self.uid,)))
        store.write(lambda c: c.execute("DELETE FROM memory_events WHERE user_id=?", (self.uid,)))
        memory._activated_mark.clear()

    # ── write / update / delete round trip ──────────────────────────────
    def test_remember_update_recall_forget_round_trip(self):
        r = memory._remember(self.session, "preference", "likes tea", tags=["drink"])
        self.assertTrue(r["ok"])
        mid = r["memory_id"]

        rec = memory._recall(self.session, query="tea")
        self.assertEqual(len(rec["memories"]), 1)
        self.assertEqual(rec["memories"][0]["value"], "likes tea")
        self.assertEqual(rec["memories"][0]["tags"], ["drink"])

        u = memory._update_memory(self.session, mid, value="likes green tea")
        self.assertTrue(u["ok"])
        rec2 = memory._recall(self.session, query="green")
        self.assertEqual(rec2["memories"][0]["value"], "likes green tea")

        f = memory._forget(self.session, mid)
        self.assertEqual(f, {"ok": True})
        self.assertEqual(memory._recall(self.session, query="tea")["memories"], [])

    def test_recall_filters_by_type_and_tag(self):
        memory._remember(self.session, "household", "recycling Tuesdays", tags=["schedule"])
        memory._remember(self.session, "preference", "vegetarian weekdays", tags=["food"])
        by_type = memory._recall(self.session, types=["household"])
        self.assertEqual(len(by_type["memories"]), 1)
        self.assertEqual(by_type["memories"][0]["type"], "household")
        by_tag = memory._recall(self.session, tags=["food"])
        self.assertEqual(len(by_tag["memories"]), 1)
        self.assertEqual(by_tag["memories"][0]["value"], "vegetarian weekdays")

    def test_reflection_add_and_update_go_through_the_same_path(self):
        import chat
        import conversation
        conversation.add_message(self.uid, "user", "by the way, people call me Sam now")
        with patch.object(chat, "call", return_value={
                "content": json.dumps({"add": [{"type": "identity", "value": "goes by Sam", "tags": []}],
                                       "update": [], "flag_removal": []})}):
            out = memory.reflect(self.uid)
        self.assertEqual(out, {"added": 1, "updated": 0, "flagged": 0})
        rows = memory.all_rows(self.uid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "reflection")
        mid = rows[0]["id"]
        with patch.object(chat, "call", return_value={
                "content": json.dumps({"add": [], "update": [{"id": mid, "value": "goes by Sammy"}],
                                       "flag_removal": []})}):
            out2 = memory.reflect(self.uid)
        self.assertEqual(out2, {"added": 0, "updated": 1, "flagged": 0})
        self.assertEqual(memory.all_rows(self.uid)[0]["value"], "goes by Sammy")

    # ── context_block: always-load types, budget-capped ─────────────────
    def test_context_block_only_always_load_types_ordered_recent_first(self):
        memory._remember(self.session, "identity", "older identity fact")
        time.sleep(0.01)
        memory._remember(self.session, "preference", "newer preference fact")
        memory._remember(self.session, "household", "not in the always-load slice")
        block = memory.context_block(self.uid)
        self.assertIn("newer preference fact", block)
        self.assertIn("older identity fact", block)
        self.assertNotIn("not in the always-load slice", block)
        self.assertLess(block.index("newer preference fact"), block.index("older identity fact"))

    def test_context_block_empty_when_nothing_always_load(self):
        memory._remember(self.session, "household", "only a household fact")
        self.assertEqual(memory.context_block(self.uid), "")

    # ── pins_precheck_line ───────────────────────────────────────────────
    def test_pins_precheck_line_none_when_nothing_pinned(self):
        memory._remember(self.session, "preference", "unpinned fact")
        self.assertIsNone(memory.pins_precheck_line(self.session, self.uid))

    def test_pins_precheck_line_lists_pinned_facts(self):
        r = memory._remember(self.session, "preference", "a pinned fact")
        memory._pin_memory(self.session, r["memory_id"], True)
        line = memory.pins_precheck_line(self.session, self.uid)
        self.assertIn("a pinned fact", line)
        self.assertTrue(line.startswith("Pinned facts (always relevant"))

    def test_set_pinned_and_set_safety_tier_settings_page_paths(self):
        r = memory._remember(self.session, "preference", "a fact")
        mid = r["memory_id"]
        self.assertEqual(memory.set_pinned(self.uid, mid, True), {"ok": True})
        self.assertTrue(memory._recall(self.session, query="a fact")["memories"][0]["pinned"])
        self.assertEqual(memory.set_safety_tier(self.uid, mid, True), {"ok": True})
        self.assertTrue(memory._recall(self.session, query="a fact")["memories"][0]["safety"])
        self.assertEqual(memory.set_pinned(self.uid, 999999, True), {"error": "no such memory"})

    # ── all_rows ──────────────────────────────────────────────────────────
    def test_all_rows_returns_every_type_unbounded(self):
        memory._remember(self.session, "task", "call the dentist")
        memory._remember(self.session, "meals", "no shellfish")
        rows = memory.all_rows(self.uid)
        self.assertEqual({r["type"] for r in rows}, {"task", "meals"})

    # ── topic_activation_line: keyword path ──────────────────────────────
    def test_topic_activation_fires_on_a_keyword_hit(self):
        import conversation
        memory._remember(self.session, "household", "the cage stays locked at all times", tags=["cage", "safety"])
        conversation.add_message(self.uid, "user", "did you check the cage today")
        line = memory.topic_activation_line(self.session, self.uid)
        self.assertIsNotNone(line)
        self.assertIn("cage stays locked", line)

    def test_topic_activation_is_silent_on_a_repeat_call_for_the_same_message(self):
        import conversation
        memory._remember(self.session, "household", "the cage stays locked", tags=["cage"])
        conversation.add_message(self.uid, "user", "checking the cage")
        first = memory.topic_activation_line(self.session, self.uid)
        self.assertIsNotNone(first)
        second = memory.topic_activation_line(self.session, self.uid)
        self.assertIsNone(second)

    def test_topic_activation_semantic_fallback_for_a_safety_tier_paraphrase(self):
        import conversation
        r = memory._remember(self.session, "household", "cage lock rule", tags=["household", "safety"], safety=True)
        conversation.add_message(self.uid, "user", "is the enclosure secured")
        embed = _fake_embed({"is the enclosure secured": [1.0, 0.0, 0.0],
                             "cage lock rule": [1.0, 0.0, 0.0]})
        with patch("chat.openrouter_embed", embed):
            line = memory.topic_activation_line(self.session, self.uid)
        self.assertIsNotNone(line)
        self.assertIn("SAFETY/CONSTRAINT", line)
        self.assertIn("cage lock rule", line)

    def test_topic_activation_degraded_when_embedding_provider_fails(self):
        import conversation
        memory._remember(self.session, "household", "a safety fact", safety=True)
        conversation.add_message(self.uid, "user", "totally unrelated content here")
        with patch("chat.openrouter_embed", return_value={"ok": False, "reason": "timeout"}):
            line = memory.topic_activation_line(self.session, self.uid)
        self.assertIsNotNone(line)
        self.assertIn("degraded", line)

    # ── preaction_check: unconditional semantic pass ─────────────────────
    def test_preaction_check_runs_semantic_unconditionally(self):
        memory._remember(self.session, "household", "never share the door code", tags=["access"], safety=True)
        embed = _fake_embed({"send_message unlock the front door": [1.0, 0.0, 0.0],
                             "never share the door code": [1.0, 0.0, 0.0]})
        with patch("chat.openrouter_embed", embed):
            line = memory.preaction_check(self.session, "send_message", {"text": "unlock the front door"})
        self.assertIsNotNone(line)
        self.assertIn("never share the door code", line)

    def test_preaction_check_none_when_nothing_relevant(self):
        memory._remember(self.session, "household", "unrelated fact", safety=True)
        embed = _fake_embed({"some_tool zzz totally unrelated": [1.0, 0.0, 0.0],
                             "unrelated fact": [0.0, 1.0, 0.0]})
        with patch("chat.openrouter_embed", embed):
            line = memory.preaction_check(self.session, "some_tool", {"arg": "zzz totally unrelated"})
        self.assertIsNone(line)


if __name__ == "__main__":
    unittest.main()

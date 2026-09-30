# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Topic-triggered memory activation (2026-09-17) -- real DB, real
scoring/extraction code, with only the network call (chat.openrouter_embed)
mocked -- there's no live embeddings endpoint in this harness. Covers:
topic_match's own pure scorer, remember/update_memory's new safety
argument, the settings-page safety toggle, topic_activation_line's
keyword+cost-gated-semantic behavior and its per-message collapse, and
preaction_check's UNCONDITIONAL semantic pass (the one place a confidence
gate must never apply) plus its degraded-provider behavior.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_topic_memory_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_topic_memory_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import chat  # noqa: E402
import conversation  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402
import tools  # noqa: E402
import topic_match  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")
_SESSION = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"}

# Registered once, at module scope, not inside a test method -- unittest
# doesn't guarantee class/method run order, and a tool registered inside
# one TestCase's own test wouldn't reliably exist yet for another.
tools.register(tools.Tool("test_consequential_tool", {"type": "function", "function": {
    "name": "test_consequential_tool", "parameters": {"type": "object", "properties": {}}}},
    lambda session: {"ok": True}, consequential=True))
tools.register(tools.Tool("test_ordinary_tool", {"type": "function", "function": {
    "name": "test_ordinary_tool", "parameters": {"type": "object", "properties": {}}}},
    lambda session: {"ok": True}))


def _fake_embed(vectors_by_text):
    """A chat.openrouter_embed stand-in -- returns a deterministic vector
    per input text (falls back to a hash-based one for text not named
    explicitly) instead of ever making a real network call."""
    def _call(texts, *, model):
        out = []
        for t in texts:
            if t in vectors_by_text:
                out.append(vectors_by_text[t])
            else:
                h = abs(hash(t)) % 1000
                out.append([h / 1000.0, 1 - h / 1000.0, 0.0])
        return {"ok": True, "vectors": out}
    return _call


class TopicMatchScorer(unittest.TestCase):
    """Pure math, no DB -- the exact formula/threshold measured for real
    against Nori's own history (see topic_match.py's module docstring)."""

    def test_stemmer_handles_both_inflections_the_same(self):
        self.assertEqual(topic_match._stem("meeting"), topic_match._stem("meetings"))

    def test_tag_hit_alone_clears_threshold(self):
        score, tag_hits, content_hits = topic_match.score(["cage"], ["household", "cage"], "keep it locked")
        self.assertGreaterEqual(score, 3.0)
        self.assertEqual(tag_hits, 1)

    def test_single_content_hit_alone_is_weaker(self):
        score, tag_hits, content_hits = topic_match.score(["mcp"], ["household"], "the mcp thing")
        self.assertLess(score, 3.0)

    def test_zero_overlap_scores_zero(self):
        score, _, _ = topic_match.score(["nodrya"], ["household"], "completely unrelated text")
        self.assertEqual(score, 0.0)


class SafetyTierAndRecall(unittest.TestCase):
    def test_remember_safety_flag_persists_and_recalls(self):
        res = memory._remember(_SESSION, "preference", "Never leave the cage unlocked overnight",
                               tags=["household", "safety"], safety=True)
        self.assertTrue(res.get("ok"), res)
        recalled = memory._recall(_SESSION, query="cage")
        rows = [m for m in recalled["memories"] if m["id"] == res["memory_id"]]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["safety"])

    def test_settings_toggle_flips_it_both_ways(self):
        res = memory._remember(_SESSION, "task", "ordinary fact, not a boundary", safety=False)
        mid = res["memory_id"]
        memory.set_safety_tier(_user["id"], mid, True)
        self.assertTrue(memory.all_rows(_user["id"])[0]["safety_tier"] or
                        any(r["id"] == mid and r["safety_tier"] for r in memory.all_rows(_user["id"])))
        memory.set_safety_tier(_user["id"], mid, False)
        self.assertFalse(any(r["id"] == mid and r["safety_tier"] for r in memory.all_rows(_user["id"])))


class ToolConsequentialFlag(unittest.TestCase):
    def test_is_consequential_true_for_flagged_tool_false_otherwise(self):
        self.assertTrue(tools.is_consequential("test_consequential_tool"))
        self.assertFalse(tools.is_consequential("test_ordinary_tool"))
        self.assertFalse(tools.is_consequential("no_such_tool"))


class TopicActivationLine(unittest.TestCase):
    def setUp(self):
        self.user = _user
        self.session = _SESSION
        memory._activated_mark.pop(self.user["id"], None)

    def test_keyword_hit_activates_and_is_labeled_safety(self):
        memory._remember(self.session, "household", "The cage must always stay locked at night",
                         tags=["household", "safety", "cage"], safety=True)
        conversation.add_message(self.user["id"], "user", "Is the cage locked right now?")
        with patch.object(chat, "openrouter_embed", side_effect=AssertionError(
                "must not call the embedding API when a keyword hit already found the safety tier")):
            line = memory.topic_activation_line(self.session, self.user["id"])
        self.assertIsNotNone(line)
        self.assertIn("SAFETY/CONSTRAINT", line)
        self.assertIn("cage", line.lower())

    def test_collapses_to_one_activation_per_message(self):
        memory._remember(self.session, "household", "The cage must always stay locked at night",
                         tags=["household", "safety", "cage"], safety=True)
        conversation.add_message(self.user["id"], "user", "What about the cage?")
        first = memory.topic_activation_line(self.session, self.user["id"])
        second = memory.topic_activation_line(self.session, self.user["id"])
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_no_topics_no_matches_returns_none(self):
        conversation.add_message(self.user["id"], "user", "the a to is it")  # all stopwords -> zero topics
        line = memory.topic_activation_line(self.session, self.user["id"])
        self.assertIsNone(line)

    def test_low_keyword_confidence_falls_back_to_semantic_pass(self):
        # A safety memory whose wording shares NO tokens with the message
        # -- the exact paraphrase-miss shape the real measurement found --
        # only surfaces via the (here, mocked) semantic pass.
        res = memory._remember(self.session, "household", "cage lock stays engaged overnight",
                               tags=["household safety"], safety=True)
        mid = res["memory_id"]
        conversation.add_message(self.user["id"], "user", "should the enclosure latch be secured now")
        embed = _fake_embed({
            "should the enclosure latch be secured now": [1.0, 0.0, 0.0],
            "cage lock stays engaged overnight": [0.99, 0.01, 0.0],
        })
        with patch.object(chat, "openrouter_embed", side_effect=embed):
            line = memory.topic_activation_line(self.session, self.user["id"])
        self.assertIsNotNone(line)
        self.assertIn("SAFETY/CONSTRAINT", line)

    def test_embedding_failure_marks_degraded_not_silent_all_clear(self):
        memory._remember(self.session, "household", "cage lock stays engaged overnight",
                         tags=["household safety"], safety=True)
        conversation.add_message(self.user["id"], "user", "totally unrelated wording here")
        with patch.object(chat, "openrouter_embed", return_value={"ok": False, "reason": "timed out"}):
            line = memory.topic_activation_line(self.session, self.user["id"])
        self.assertIsNotNone(line)
        self.assertIn("degraded", line.lower())


class PreactionCheckUnconditionalSemantic(unittest.TestCase):
    def setUp(self):
        self.user = _user
        self.session = _SESSION

    def test_semantic_pass_runs_even_when_keyword_already_confident(self):
        """The refinement insisted on: point-6 moments never gate the
        semantic pass on keyword confidence, because a confident keyword
        hit can still be the WRONG safety memory. Assert the embedding
        call happens even though a keyword hit already exists."""
        memory._remember(self.session, "household", "smart lock permissions: never grant a peer full trust",
                         tags=["household", "safety", "permissions"], safety=True)
        calls = []

        def _spy(texts, *, model):
            calls.append(texts)
            return {"ok": True, "vectors": [[0.1, 0.2, 0.3] for _ in texts]}

        with patch.object(chat, "openrouter_embed", side_effect=_spy):
            note = memory.preaction_check(self.session, "enable_mcp_connection", {"server_id": 7})
        self.assertTrue(calls, "openrouter_embed was never called -- the semantic pass was gated, which is exactly what point 6 forbids")

    def test_degraded_provider_is_visible_not_silent(self):
        memory._remember(self.session, "household", "never disable the front-door lock automation",
                         tags=["household", "safety"], safety=True)
        with patch.object(chat, "openrouter_embed", return_value={"ok": False, "reason": "connection refused"}):
            note = memory.preaction_check(self.session, "ha_control", {"entity_id": "lock.front_door", "action": "unlock"})
        self.assertIsNotNone(note)
        self.assertIn("degraded", note.lower())

    def test_no_relevant_memory_returns_none_but_is_still_logged(self):
        with patch.object(chat, "openrouter_embed", return_value={"ok": True, "vectors": [[0.0, 0.0, 0.0]]}):
            note = memory.preaction_check(self.session, "update_settings", {"field": "quiet_hours_start", "value": 22})
        self.assertIsNone(note)


class ChatLoopWiresThePreactionHook(unittest.TestCase):
    """End-to-end through chat.run()'s real tool-calling loop -- not just a
    direct call to preaction_check -- so a future refactor of that loop
    can't silently unwire the hook without a test noticing. The model call
    itself is mocked (no live endpoint in this harness); tools.dispatch is
    real, calling the real test_consequential_tool registered above."""

    def test_consequential_tool_call_triggers_preaction_check(self):
        rounds = [
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "test_consequential_tool", "arguments": "{}"}}], "usage": {}},
            {"content": "done", "tool_calls": [], "usage": {}},
        ]
        with patch.object(chat, "call_via_chain", side_effect=rounds), \
             patch.object(memory, "preaction_check", return_value=None) as spy:
            result = chat.run(_SESSION, _user["id"], _user["display_name"], max_rounds=4)
        self.assertEqual(result["text"], "done")
        spy.assert_called_once()
        called_name = spy.call_args[0][1]
        self.assertEqual(called_name, "test_consequential_tool")

    def test_ordinary_tool_call_does_not_trigger_preaction_check(self):
        rounds = [
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "test_ordinary_tool", "arguments": "{}"}}], "usage": {}},
            {"content": "done", "tool_calls": [], "usage": {}},
        ]
        with patch.object(chat, "call_via_chain", side_effect=rounds), \
             patch.object(memory, "preaction_check", return_value=None) as spy:
            chat.run(_SESSION, _user["id"], _user["display_name"], max_rounds=4)
        spy.assert_not_called()


if __name__ == "__main__":
    unittest.main()

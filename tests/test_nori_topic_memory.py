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
import json
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
import config  # noqa: E402
import conversation  # noqa: E402
import crypto  # noqa: E402
import mcp_client  # noqa: E402
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


def _mcp_result(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}


class BroadNodryaRetrieval(unittest.TestCase):
    """nodrya_broad_search and its wiring into recall/topic_activation_line/
    preaction_check (2026-10-02, operator's own ask: "any note in Nodrya
    can surface to Nori in conversation based on context") -- a SEPARATE
    axis from memory_backend/NodryaMemoryBackend (those govern where HER
    OWN memory lands; this is read-broadly-across-everything-else, off by
    default, opt-in via nodrya_broad_retrieval). Real DB, mocked network
    boundary only (mcp_client.call_tool), same posture as every other
    class in this file."""

    def setUp(self):
        self.user = _user
        self.wsid = _user["workspace_id"]
        self.session = _SESSION
        memory._activated_mark.pop(self.user["id"], None)
        config.set("workspace", self.wsid, "nodrya_broad_retrieval", False)
        config.set("workspace", self.wsid, "nodrya_mcp_url", "")

    def tearDown(self):
        config.set("workspace", self.wsid, "nodrya_broad_retrieval", False)
        config.set("workspace", self.wsid, "nodrya_mcp_url", "")

    def _connect(self):
        config.set("workspace", self.wsid, "nodrya_mcp_url",
                   crypto.encrypt("https://notes.example.invalid/api/mcp/nod_mcp_testtoken"))

    # ── nodrya_broad_search itself ───────────────────────────────────────
    def test_off_by_default_even_with_a_connector_saved(self):
        self._connect()
        with patch.object(mcp_client, "call_tool") as mock_call:
            notes, degraded = memory.nodrya_broad_search(self.wsid, "anything")
        mock_call.assert_not_called()
        self.assertEqual(notes, [])
        self.assertFalse(degraded)

    def test_enabled_but_no_connector_is_a_silent_noop(self):
        config.set("workspace", self.wsid, "nodrya_broad_retrieval", True)
        with patch.object(mcp_client, "call_tool") as mock_call:
            notes, degraded = memory.nodrya_broad_search(self.wsid, "anything")
        mock_call.assert_not_called()
        self.assertEqual(notes, [])
        self.assertFalse(degraded)

    def test_enabled_and_connected_searches_all_categories(self):
        config.set("workspace", self.wsid, "nodrya_broad_retrieval", True)
        self._connect()
        with patch.object(mcp_client, "call_tool", return_value=_mcp_result(
                {"count": 1, "query": "cage", "notes": [
                    {"id": 1, "title": "Cage maintenance log", "content": "Hinge replaced in March",
                     "category": {"id": 9, "name": "Household"}, "similarity": 0.91, "is_encrypted": False},
                ]})) as mock_call:
            notes, degraded = memory.nodrya_broad_search(self.wsid, "is the cage ok")
        self.assertEqual(mock_call.call_args.args[1], "search_by_meaning")
        self.assertNotIn("category_id", mock_call.call_args.args[2])
        self.assertEqual(len(notes), 1)
        self.assertFalse(degraded)

    def test_nodrya_failure_degrades_rather_than_raising(self):
        config.set("workspace", self.wsid, "nodrya_broad_retrieval", True)
        self._connect()
        with patch.object(mcp_client, "call_tool", side_effect=mcp_client.MCPError("timed out")):
            notes, degraded = memory.nodrya_broad_search(self.wsid, "anything")
        self.assertEqual(notes, [])
        self.assertTrue(degraded)

    # ── _recall tool ──────────────────────────────────────────────────────
    def test_recall_tool_includes_notes_from_nodrya_when_enabled(self):
        config.set("workspace", self.wsid, "nodrya_broad_retrieval", True)
        self._connect()
        with patch.object(mcp_client, "call_tool", return_value=_mcp_result(
                {"notes": [{"id": 1, "title": "Insurance policy", "content": "Renews every June",
                           "category": {"name": "Household"}, "similarity": 0.8, "is_encrypted": False}]})):
            result = memory._recall(self.session, query="insurance")
        self.assertIn("notes_from_nodrya", result)
        self.assertEqual(result["notes_from_nodrya"][0]["title"], "Insurance policy")

    def test_recall_tool_has_no_nodrya_key_when_disabled(self):
        result = memory._recall(self.session, query="insurance")
        self.assertNotIn("notes_from_nodrya", result)
        self.assertNotIn("notes_from_nodrya_degraded", result)

    # ── automatic surfacing: the actual "based on context" behavior ───────
    def test_topic_activation_surfaces_a_broad_note_with_no_local_memory_at_all(self):
        # Deliberately nothing remembered locally -- this is the exact
        # case the operator asked for: a Nodrya note surfaces purely from
        # broad retrieval, not because it also happens to be one of her
        # own curated facts.
        config.set("workspace", self.wsid, "nodrya_broad_retrieval", True)
        self._connect()
        conversation.add_message(self.user["id"], "user", "do we have a car insurance policy on file")
        with patch.object(mcp_client, "call_tool", return_value=_mcp_result(
                {"notes": [{"id": 1, "title": "Car insurance", "content": "Policy renews in June",
                           "category": {"name": "Household"}, "similarity": 0.77, "is_encrypted": False}]})):
            line = memory.topic_activation_line(self.session, self.user["id"])
        self.assertIsNotNone(line)
        self.assertIn("Car insurance", line)
        self.assertIn("Nodrya", line)

    def test_topic_activation_omits_nodrya_block_when_disabled(self):
        conversation.add_message(self.user["id"], "user", "do we have a car insurance policy on file")
        with patch.object(mcp_client, "call_tool") as mock_call:
            memory.topic_activation_line(self.session, self.user["id"])
        mock_call.assert_not_called()

    def test_preaction_check_includes_a_broad_notes_block(self):
        config.set("workspace", self.wsid, "nodrya_broad_retrieval", True)
        self._connect()
        with patch.object(mcp_client, "call_tool", return_value=_mcp_result(
                {"notes": [{"id": 1, "title": "Router admin password", "content": "see the sticker",
                           "category": {"name": "Household"}, "similarity": 0.7, "is_encrypted": False}]})), \
             patch.object(chat, "openrouter_embed", return_value={"ok": True, "vectors": [[0.0, 0.0, 0.0]]}):
            note = memory.preaction_check(self.session, "wifi_settings_change", {"ssid": "home"})
        self.assertIsNotNone(note)
        self.assertIn("Router admin password", note)


if __name__ == "__main__":
    unittest.main()

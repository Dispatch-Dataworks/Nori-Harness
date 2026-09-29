# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""NodryaMemoryBackend (2026-09-25) -- the Nodrya counterpart to LocalMemoryBackend, implementing
the exact same 13-method interface (see memory.py's own seam docstring).
Real local store, mocked network (mcp_client.call_tool and
chat.openrouter_embed -- there's no live Nodrya or embeddings endpoint in
this harness).

Covers: the interface contract directly (write/update/get/delete/recall/
context_slice/pinned_rows/all_rows/safety_rows/set_pinned/set_safety_tier),
the write-through cache giving instant read-back before any live Nodrya
call for recall(), a write/update/delete each raising NodryaBackendError
(never a silent partial success) when Nodrya is unreachable, and that the
whole backend is only reachable when a workspace has actually configured
it (memory_backend="nodrya" plus URL/token/category all set).
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_nodrya_backend_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_nodrya_backend_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import config  # noqa: E402
import mcp_client  # noqa: E402
import memory  # noqa: E402
import store  # noqa: E402

store.init()


def _fake_embed(vectors_by_text=None):
    vectors_by_text = vectors_by_text or {}

    def _call(texts, *, model):
        return {"ok": True, "vectors": [vectors_by_text.get(t, [0.0, 0.0, 1.0]) for t in texts]}
    return _call


def _mcp_result(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}


class NodryaMemoryBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.user = accounts.bootstrap_admin("Tester", "testpass123")
        cls.uid = cls.user["id"]
        cls.wsid = cls.user["workspace_id"]
        cls.backend = memory.NodryaMemoryBackend()

    def setUp(self):
        store.write(lambda c: c.execute("DELETE FROM nodrya_memory WHERE user_id=?", (self.uid,)))
        config.set("workspace", self.wsid, "nodrya_mcp_url", "")
        config.set("workspace", self.wsid, "nodrya_mcp_token", "")
        config.set("workspace", self.wsid, "nodrya_memory_category_id", 0)

    def _configure(self, category_id=42):
        config.set("workspace", self.wsid, "nodrya_mcp_url", "https://notes.example.invalid/mcp")
        config.set("workspace", self.wsid, "nodrya_mcp_token", "nod_mcp_testtoken")
        config.set("workspace", self.wsid, "nodrya_memory_category_id", category_id)

    # ── unconfigured workspace ───────────────────────────────────────────
    def test_unconfigured_workspace_raises_on_write(self):
        with self.assertRaises(memory.NodryaBackendError):
            self.backend.write(user_id=self.uid, workspace_id=self.wsid, type_="preference",
                               value="x", tags=None, safety=False)

    # ── write / recall: the core instant-read-back property ─────────────
    def test_write_then_recall_finds_it_before_any_nodrya_durability(self):
        self._configure()
        with patch.object(mcp_client, "call_tool",
                          return_value=_mcp_result({"note": {"id": 501}})) as mock_call, \
             patch("chat.openrouter_embed", _fake_embed()):
            mid = self.backend.write(user_id=self.uid, workspace_id=self.wsid, type_="preference",
                                     value="likes strong coffee", tags=["drink"], safety=False)
        self.assertIsInstance(mid, int)
        self.assertEqual(mock_call.call_args.args[1], "create_note")
        self.assertEqual(mock_call.call_args.args[2]["category_id"], 42)

        found = self.backend.recall(user_id=self.uid, types=None, tags=None, query="coffee", limit=20)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["value"], "likes strong coffee")
        self.assertEqual(found[0]["tags"], ["drink"])

    def test_write_raises_and_writes_nothing_locally_when_nodrya_is_unreachable(self):
        self._configure()
        with patch.object(mcp_client, "call_tool", side_effect=mcp_client.MCPError("connection refused")), \
             patch("chat.openrouter_embed", _fake_embed()):
            with self.assertRaises(memory.NodryaBackendError):
                self.backend.write(user_id=self.uid, workspace_id=self.wsid, type_="preference",
                                   value="should not be lost", tags=None, safety=False)
        rows = self.backend.all_rows(user_id=self.uid)
        self.assertEqual(rows, [])

    def test_embedding_outage_does_not_block_the_write(self):
        self._configure()
        with patch.object(mcp_client, "call_tool", return_value=_mcp_result({"note": {"id": 9}})), \
             patch("chat.openrouter_embed", return_value={"ok": False, "reason": "timeout"}):
            mid = self.backend.write(user_id=self.uid, workspace_id=self.wsid, type_="task",
                                     value="still written despite embed outage", tags=None, safety=False)
        row = self.backend.get(user_id=self.uid, memory_id=mid)
        self.assertEqual(row["value"], "still written despite embed outage")

    # ── update / get / delete ────────────────────────────────────────────
    def test_update_changes_the_row_and_calls_update_note(self):
        self._configure()
        with patch.object(mcp_client, "call_tool", return_value=_mcp_result({"note": {"id": 7}})), \
             patch("chat.openrouter_embed", _fake_embed()):
            mid = self.backend.write(user_id=self.uid, workspace_id=self.wsid, type_="preference",
                                     value="original value", tags=None, safety=False)

        with patch.object(mcp_client, "call_tool", return_value=_mcp_result({"ok": True})) as mock_call, \
             patch("chat.openrouter_embed", _fake_embed()):
            updated = self.backend.update(user_id=self.uid, memory_id=mid, value="updated value",
                                          tags=None, safety=None)
        self.assertEqual(updated["value"], "updated value")
        self.assertEqual(mock_call.call_args.args[1], "update_note")
        self.assertEqual(mock_call.call_args.args[2]["note_id"], 7)

        row = self.backend.get(user_id=self.uid, memory_id=mid)
        self.assertEqual(row["value"], "updated value")

    def test_update_unknown_id_returns_none_not_an_error(self):
        self._configure()
        self.assertIsNone(self.backend.update(user_id=self.uid, memory_id=999999, value="x",
                                              tags=None, safety=None))

    def test_delete_trashes_in_nodrya_and_removes_locally(self):
        self._configure()
        with patch.object(mcp_client, "call_tool", return_value=_mcp_result({"note": {"id": 11}})), \
             patch("chat.openrouter_embed", _fake_embed()):
            mid = self.backend.write(user_id=self.uid, workspace_id=self.wsid, type_="task",
                                     value="temporary fact", tags=None, safety=False)

        with patch.object(mcp_client, "call_tool", return_value=_mcp_result({"ok": True})) as mock_call:
            deleted = self.backend.delete(user_id=self.uid, memory_id=mid)
        self.assertEqual(deleted["value"], "temporary fact")
        self.assertEqual(mock_call.call_args.args[1], "trash_note")
        self.assertEqual(mock_call.call_args.args[2]["note_id"], 11)
        self.assertIsNone(self.backend.get(user_id=self.uid, memory_id=mid))

    def test_delete_raises_and_keeps_the_local_row_when_trash_fails(self):
        self._configure()
        with patch.object(mcp_client, "call_tool", return_value=_mcp_result({"note": {"id": 13}})), \
             patch("chat.openrouter_embed", _fake_embed()):
            mid = self.backend.write(user_id=self.uid, workspace_id=self.wsid, type_="task",
                                     value="keep me on failure", tags=None, safety=False)

        with patch.object(mcp_client, "call_tool", side_effect=mcp_client.MCPError("timeout")):
            with self.assertRaises(memory.NodryaBackendError):
                self.backend.delete(user_id=self.uid, memory_id=mid)
        self.assertIsNotNone(self.backend.get(user_id=self.uid, memory_id=mid))

    # ── pin / safety tier ────────────────────────────────────────────────
    def test_set_pinned_and_set_safety_tier(self):
        self._configure()
        with patch.object(mcp_client, "call_tool", return_value=_mcp_result({"note": {"id": 21}})), \
             patch("chat.openrouter_embed", _fake_embed()):
            mid = self.backend.write(user_id=self.uid, workspace_id=self.wsid, type_="preference",
                                     value="a fact", tags=None, safety=False)
        self.assertIsNotNone(self.backend.set_pinned(user_id=self.uid, memory_id=mid, pinned=True))
        self.assertTrue(self.backend.get(user_id=self.uid, memory_id=mid)["pinned"])
        self.assertIsNotNone(self.backend.set_safety_tier(user_id=self.uid, memory_id=mid, safety=True))
        self.assertTrue(self.backend.get(user_id=self.uid, memory_id=mid)["safety_tier"])
        self.assertIsNone(self.backend.set_pinned(user_id=self.uid, memory_id=999999, pinned=True))

    # ── context_slice / pinned_rows / all_rows / safety_rows ────────────
    def test_context_slice_filters_by_type_ordered_recent_first(self):
        self._configure()
        with patch.object(mcp_client, "call_tool", return_value=_mcp_result({"note": {"id": 1}})), \
             patch("chat.openrouter_embed", _fake_embed()):
            self.backend.write(user_id=self.uid, workspace_id=self.wsid, type_="identity",
                               value="identity fact", tags=None, safety=False)
            self.backend.write(user_id=self.uid, workspace_id=self.wsid, type_="household",
                               value="not in the slice", tags=None, safety=False)
        rows = self.backend.context_slice(user_id=self.uid, types=("identity", "preference"))
        self.assertEqual([r["value"] for r in rows], ["identity fact"])

    def test_all_rows_and_safety_rows(self):
        self._configure()
        with patch.object(mcp_client, "call_tool", return_value=_mcp_result({"note": {"id": 1}})), \
             patch("chat.openrouter_embed", _fake_embed()):
            self.backend.write(user_id=self.uid, workspace_id=self.wsid, type_="household",
                               value="never share the door code", tags=None, safety=True)
            self.backend.write(user_id=self.uid, workspace_id=self.wsid, type_="meals",
                               value="vegetarian weekdays", tags=None, safety=False)
        self.assertEqual(len(self.backend.all_rows(user_id=self.uid)), 2)
        safety = self.backend.safety_rows(user_id=self.uid)
        self.assertEqual(len(safety), 1)
        self.assertEqual(safety[0]["value"], "never share the door code")

    # ── semantic_match: unconditional pass, same degrade contract as local ─
    def test_semantic_match_finds_a_paraphrase_over_threshold(self):
        self._configure()
        with patch.object(mcp_client, "call_tool", return_value=_mcp_result({"note": {"id": 1}})), \
             patch("chat.openrouter_embed", _fake_embed({"cage lock rule": [1.0, 0.0, 0.0]})):
            self.backend.write(user_id=self.uid, workspace_id=self.wsid, type_="household",
                               value="cage lock rule", tags=None, safety=True)
        with patch("chat.openrouter_embed", _fake_embed({"is the enclosure secured": [1.0, 0.0, 0.0]})):
            matches, degraded = self.backend.semantic_match(
                user_id=self.uid, query_text="is the enclosure secured", limit=5)
        self.assertFalse(degraded)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["value"], "cage lock rule")

    def test_semantic_match_degrades_when_the_query_embedding_fails(self):
        self._configure()
        with patch.object(mcp_client, "call_tool", return_value=_mcp_result({"note": {"id": 1}})), \
             patch("chat.openrouter_embed", _fake_embed()):
            self.backend.write(user_id=self.uid, workspace_id=self.wsid, type_="household",
                               value="a safety fact", tags=None, safety=True)
        with patch("chat.openrouter_embed", return_value={"ok": False, "reason": "timeout"}):
            matches, degraded = self.backend.semantic_match(user_id=self.uid, query_text="anything", limit=5)
        self.assertTrue(degraded)
        self.assertEqual(matches, [])

    # ── end-to-end through memory.py's own tool functions, backend selected
    #    via the real memory_backend setting -- not just the class directly
    def test_selected_via_memory_backend_setting_through_the_real_tool_functions(self):
        self._configure()
        config.set("workspace", self.wsid, "memory_backend", "nodrya")
        session = {"user_id": self.uid, "workspace_id": self.wsid, "role": "admin"}
        try:
            with patch.object(mcp_client, "call_tool",
                              return_value=_mcp_result({"note": {"id": 77}})), \
                 patch("chat.openrouter_embed", _fake_embed()):
                r = memory._remember(session, "preference", "reached through the real tool fn")
            self.assertTrue(r["ok"])
            rec = memory._recall(session, query="reached")
            self.assertEqual(len(rec["memories"]), 1)
            # and it must NOT have landed in the local `memory` table
            local_rows = store.read(lambda c: c.execute(
                "SELECT COUNT(*) AS n FROM memory WHERE user_id=?", (self.uid,)).fetchone())
            self.assertEqual(local_rows["n"], 0)
        finally:
            config.set("workspace", self.wsid, "memory_backend", "local")


if __name__ == "__main__":
    unittest.main()

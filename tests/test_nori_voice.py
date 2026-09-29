# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Offline regressions for voice-conversation-mode turn handling
(2026-09-13): send_msg/_run_turn/retry_post tagging a turn's messages
with meta={"source":"voice"} when it came from the push-to-talk modal.
Never imports server.py's live .env/bootstrap or a real chat.run() --
same AST-extraction technique test_nori_chat.py uses, so this coexists
with a sibling application's same-named modules without touching either app's data.

Run: python -m unittest discover -s tests -p test_nori_voice.py -v
"""
import ast
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]


class _StubStage:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StubTurn:
    """Stands in for timing.Turn/NULL_TURN -- this test suite covers voice-
    turn metadata tagging, not timing.py itself (see test_nori_chat.py's
    own analogous stubs for other cross-cutting concerns)."""
    turn_id = None

    def stage(self, name, **extra):
        return _StubStage()

    def finish(self):
        pass


def definitions(filename, names, namespace):
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if getattr(n, "name", None) in names or
             isinstance(n, ast.Assign) and any(getattr(t, "id", None) in names for t in n.targets)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), namespace)
    return namespace


class VoiceTurnMetaTests(unittest.TestCase):
    def setUp(self):
        self.rows = {}
        self.next_id = 1

        def add_message(user_id, role, content, **kwargs):
            mid = self.next_id
            self.next_id += 1
            self.rows[mid] = {"id": mid, "role": role, "content": content, **kwargs}
            return mid

        self.add_message = Mock(side_effect=add_message)
        self.chat_run = Mock(return_value={
            "text": "Hello there.",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.002}})

        env = dict(
            json=json,
            conversation=SimpleNamespace(
                add_message=self.add_message,
                max_id=lambda uid: max(self.rows) if self.rows else 0,
                since=lambda uid, after, include_tool=True: [],
                cost_meta=lambda usage: {
                    "cost_usd": usage.get("cost"), "cost_unavailable": usage.get("cost") is None,
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0)},
                get_own=lambda uid, mid: self.rows.get(mid),
                has_reply_after=lambda uid, mid: False,
            ),
            chat=SimpleNamespace(run=self.chat_run, ModelError=RuntimeError),
            turns=SimpleNamespace(run=lambda user_id, first, sweep: first()),
            emotion=SimpleNamespace(get_state=lambda uid: "neutral"),
            accounts=SimpleNamespace(get_user=lambda uid: {"id": uid, "display_name": "Test",
                                                            "workspace_id": 1}),
            config=SimpleNamespace(get=lambda *a: 12),
            timing=SimpleNamespace(start=lambda *a, **kw: _StubTurn(), NULL_TURN=_StubTurn()),
        )
        tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
        handler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
        handler.bases = []
        handler.body = [n for n in handler.body if getattr(n, "name", None) in {
            "_run_turn", "send_msg", "retry_post", "send_json"}]
        exec(compile(ast.Module(body=[handler], type_ignores=[]), "server.py", "exec"), env)
        self.instance = env["Handler"]()
        self.instance.send = Mock()
        # send_msg/retry_post now ride board_count along in the same JSON
        # response (2026-09-17, needs-attention badge) -- board state is
        # unrelated to what this suite covers (voice-turn meta tagging), so
        # stub it directly rather than pulling in tasks/notes/reminders.
        self.instance._board_count = lambda sess: 0
        self.sess = {"user_id": 1, "csrf": "t"}

    def _sent_json(self):
        return json.loads(self.instance.send.call_args.args[1])

    def test_voice_send_tags_both_user_and_assistant_meta(self):
        self.instance.send_msg(self.sess, {"text": "what's the weather", "source": "voice"})
        payload = self._sent_json()
        self.assertTrue(payload["ok"])
        user_call = self.add_message.call_args_list[0]
        self.assertEqual(user_call.args, (1, "user", "what's the weather"))
        self.assertEqual(user_call.kwargs.get("meta"), {"source": "voice"})
        assistant_call = self.add_message.call_args_list[1]
        self.assertEqual(assistant_call.kwargs["meta"]["source"], "voice")
        # cost fields still ride along in the same record, not a second write
        self.assertIn("cost_usd", assistant_call.kwargs["meta"])

    def test_typed_send_has_no_source_tag_at_all(self):
        self.instance.send_msg(self.sess, {"text": "hey"})
        user_call = self.add_message.call_args_list[0]
        self.assertIsNone(user_call.kwargs.get("meta"))
        assistant_call = self.add_message.call_args_list[1]
        self.assertNotIn("source", assistant_call.kwargs["meta"])

    def test_empty_stt_text_never_reaches_send_msg_as_a_stored_message(self):
        # send_msg's own blank-text guard -- the same one typing an empty
        # string into the composer already hit; voice mode relies on this
        # by simply never calling sendText when STT returned nothing.
        self.instance.send_msg(self.sess, {"text": "", "source": "voice"})
        self.add_message.assert_not_called()

    def test_retry_of_a_voice_turn_keeps_the_voice_tag_without_a_source_field(self):
        self.rows[5] = {"id": 5, "role": "user", "content": "what's for dinner",
                        "meta": json.dumps({"source": "voice"})}
        self.instance.retry_post(self.sess, {"user_id": "5"})
        assistant_call = self.add_message.call_args_list[0]
        self.assertEqual(assistant_call.kwargs["meta"]["source"], "voice")

    def test_retry_of_a_typed_turn_stays_untagged(self):
        self.rows[6] = {"id": 6, "role": "user", "content": "what's for dinner", "meta": None}
        self.instance.retry_post(self.sess, {"user_id": "6"})
        assistant_call = self.add_message.call_args_list[0]
        self.assertNotIn("source", assistant_call.kwargs["meta"])


if __name__ == "__main__":
    unittest.main()

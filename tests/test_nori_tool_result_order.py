# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Tool results stay together (2026-10-28, a real failure on Iron / Claude Haiku 5.5):

    Anthropic call failed (400): messages.12: `tool_use` ids were found without `tool_result` blocks immediately after

Cause: the model made two tool calls in one message; after the FIRST one's result, chat.run appended a system note (the memory check that follows a
consequential tool), so the note sat between the two results. The adapters used to hoist every system message to the top, which hid it; once they kept
mid-conversation system messages in place, it split the results and the provider refused the request.

Pinned: the loop no longer appends a note until every result of the round is in; the one choke point every provider call goes through puts any note
that did land inside a run of results after them (and leaves a well-ordered list equal); and the Anthropic translation satisfies Anthropic's own rule
(checked here by a validator of that rule, not by eye), with parallel results in one user message."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_toolorder_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_toolorder_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"
for _f in ("persona.default.md", "persona.template.md"):
    shutil.copy(ROOT / "prompts" / _f, os.environ["NORI_PROMPTS_DIR"])

import accounts  # noqa: E402
import chat  # noqa: E402
import context  # noqa: E402
import memory  # noqa: E402
import precheck  # noqa: E402
import store  # noqa: E402
import tools  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Ben", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
SESSION = {"user_id": UID, "workspace_id": WS, "role": "admin", "csrf": "x"}


def _call(cid, name="remember"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _tool(cid, content="{}"):
    return {"role": "tool", "tool_call_id": cid, "content": content}


def _note(text="memory check"):
    return {"role": "system", "content": text}


def _split_run():
    """The exact shape that failed: two parallel calls, a note after the first result."""
    return [
        {"role": "system", "content": "persona"},
        {"role": "user", "content": "save both of those"},
        {"role": "assistant", "content": None, "tool_calls": [_call("toolu_A"), _call("toolu_B")]},
        _tool("toolu_A"), _note(), _tool("toolu_B"),
    ]


def anthropic_violations(anth_messages):
    """Anthropic's rule: every tool_use in an assistant message needs a tool_result for it in the very next message (a user message), all of them there."""
    bad = []
    for i, m in enumerate(anth_messages):
        if m["role"] != "assistant" or not isinstance(m["content"], list):
            continue
        ids = [b["id"] for b in m["content"] if b.get("type") == "tool_use"]
        if not ids:
            continue
        nxt = anth_messages[i + 1] if i + 1 < len(anth_messages) else None
        got = {b.get("tool_use_id") for b in (nxt["content"] if nxt and nxt["role"] == "user" and isinstance(nxt["content"], list) else [])
               if b.get("type") == "tool_result"}
        if not set(ids) <= got:
            bad.append((i, ids, sorted(got)))
    return bad


class Settle(unittest.TestCase):
    def test_a_note_in_the_middle_of_the_results_moves_after_them(self):
        out = chat._settle_tool_results(_split_run())
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant", "tool", "tool", "system"])
        self.assertEqual([m["tool_call_id"] for m in out if m["role"] == "tool"], ["toolu_A", "toolu_B"], "results keep their order")

    def test_an_ordered_list_comes_back_equal(self):
        ok = [{"role": "system", "content": "p"}, {"role": "user", "content": "hi"},
              {"role": "assistant", "content": None, "tool_calls": [_call("a")]}, _tool("a"), _note(),
              {"role": "assistant", "content": "done"}]
        self.assertEqual(chat._settle_tool_results(ok), ok)

    def test_it_does_not_mutate_its_input(self):
        msgs = _split_run()
        before = [dict(m) for m in msgs]
        chat._settle_tool_results(msgs)
        self.assertEqual(msgs, before)

    def test_the_run_ends_at_the_next_user_or_assistant_message(self):
        msgs = _split_run() + [{"role": "assistant", "content": "ok"}, _note("later note"), {"role": "user", "content": "thanks"}]
        out = chat._settle_tool_results(msgs)
        self.assertEqual(out[-3:], msgs[-3:], "what comes after the run is untouched")

    def test_several_rounds_are_each_settled(self):
        msgs = [{"role": "user", "content": "go"},
                {"role": "assistant", "content": None, "tool_calls": [_call("a"), _call("b")]}, _tool("a"), _note("n1"), _tool("b"),
                {"role": "assistant", "content": None, "tool_calls": [_call("c"), _call("d")]}, _tool("c"), _note("n2"), _tool("d")]
        out = chat._settle_tool_results(msgs)
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "tool", "tool", "system", "assistant", "tool", "tool", "system"])

    def test_no_tools_no_change(self):
        msgs = [{"role": "system", "content": "p"}, {"role": "user", "content": "hi"}, _note("mid")]
        self.assertEqual(chat._settle_tool_results(msgs), msgs)


class AnthropicTranslation(unittest.TestCase):
    def test_the_failing_shape_violates_the_rule_without_the_fix(self):
        """Guards the validator itself: the unfixed shape must be flagged, or the checks below prove nothing."""
        _sys, out = chat._messages_to_anthropic(_split_run())
        self.assertTrue(anthropic_violations(out), "the validator must catch a note between the results")

    def test_after_settling_the_request_satisfies_anthropics_rule(self):
        _sys, out = chat._messages_to_anthropic(chat._settle_tool_results(_split_run()))
        self.assertEqual(anthropic_violations(out), [])

    def test_parallel_results_share_one_user_message_then_the_note(self):
        _sys, out = chat._messages_to_anthropic(chat._settle_tool_results(_split_run()))
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "user", "user"])
        self.assertEqual([b["tool_use_id"] for b in out[2]["content"]], ["toolu_A", "toolu_B"])
        self.assertTrue(out[3]["content"].startswith(chat._APP_NOTE_PREFIX))

    def test_a_single_result_is_unchanged(self):
        msgs = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "ok", "tool_calls": [_call("a")]}, _tool("a", '{"ok": true}')]
        _s, out = chat._messages_to_anthropic(msgs)
        self.assertEqual(out[2], {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": '{"ok": true}'}]})
        self.assertEqual(anthropic_violations(out), [])

    def test_a_tool_result_after_a_plain_user_message_does_not_get_merged_into_it(self):
        msgs = [{"role": "user", "content": "x"}, {"role": "assistant", "content": None, "tool_calls": [_call("a")]}, _tool("a")]
        _s, out = chat._messages_to_anthropic(msgs)
        self.assertEqual(out[0], {"role": "user", "content": "x"})


class ChokePoint(unittest.TestCase):
    def test_every_provider_call_gets_settled_messages(self):
        seen = {}

        def fake_caller(entry, messages, **kw):
            seen["messages"] = messages
            return {"content": "ok", "tool_calls": [], "usage": {}}

        entry = {"alias": "x", "model_name": "m", "reasoning_effort": None, "provider": {"type": "fake", "label": "f"}}
        with patch.dict(chat._PROVIDER_CALLERS, {"fake": fake_caller}):
            chat.call_for_model(entry, _split_run())
        self.assertEqual([m["role"] for m in seen["messages"]], ["system", "user", "assistant", "tool", "tool", "system"])

    def test_the_anthropic_request_actually_sent_is_valid(self):
        """Through call_for_model into the real Anthropic request builder, with only the HTTP call faked."""
        sent = {}

        def fake_http(fn, req, timeout, **kw):
            import json
            sent["body"] = json.loads(req.data.decode("utf-8"))
            return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {}, "id": "r"}

        entry = {"alias": "Haiku", "model_name": "claude-haiku", "reasoning_effort": None,
                 "provider": {"type": "anthropic_api_key", "label": "A", "api_key_enc": "x", "status": "connected", "enabled": 1}}
        with patch.object(chat, "_run_call", fake_http), patch("providers.api_key_for", return_value="k"):
            chat.call_for_model(entry, _split_run())
        self.assertEqual(anthropic_violations(sent["body"]["messages"]), [])


class RunLoop(unittest.TestCase):
    """The loop itself: with a consequential tool and a memory note, two parallel calls leave the results together."""

    def test_the_note_waits_until_every_result_is_in(self):
        calls_seen = []
        replies = [
            {"content": "", "usage": {}, "tool_calls": [_call("toolu_A"), _call("toolu_B")]},
            {"content": "saved both", "usage": {}, "tool_calls": []},
        ]

        def fake_chain(messages, workspace_id, **kw):
            calls_seen.append([dict(m) for m in messages])
            return replies.pop(0)

        base = [{"role": "system", "content": "persona"}, {"role": "user", "content": "save both"}]
        with patch.object(context, "build_messages", return_value=list(base)), patch.object(precheck, "build_block", return_value=None), \
                patch.object(tools, "active_schemas", return_value=[]), patch.object(tools, "dispatch", return_value={"ok": True}), \
                patch.object(tools, "is_consequential", return_value=True), \
                patch.object(memory, "preaction_check", return_value="a related memory"), \
                patch.object(chat, "call_via_chain", fake_chain):
            out = chat.run(SESSION, UID, "Ben")
        self.assertEqual(out["text"], "saved both")
        second = calls_seen[1]
        self.assertEqual([m["role"] for m in second], ["system", "user", "assistant", "tool", "tool", "system", "system"])
        self.assertEqual([m["tool_call_id"] for m in second if m["role"] == "tool"], ["toolu_A", "toolu_B"])
        self.assertEqual([m["content"] for m in second if m["role"] == "system"][1:], ["a related memory", "a related memory"],
                         "one note per consequential call, all after the results")


if __name__ == "__main__":
    unittest.main()

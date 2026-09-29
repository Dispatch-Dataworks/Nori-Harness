# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Offline Nori regressions; never import server.py's live .env/bootstrap.

Extract the actual rendering/turn functions with AST so these tests can
coexist with a sibling application's same-named modules without touching either app's data.
Use --fixture PATH to render a synthetic chat for nori_chat_ui.cjs.
"""
import ast
import html
import json
from pathlib import Path
import re
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import call as mock_call, Mock

ROOT = Path(__file__).resolve().parents[1]


class _NullCtx:
    """Stands in for timing.py's own no-op stage context manager -- these
    tests cover chat.run()'s tool-calling loop, not timing.py itself."""
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def definitions(filename, names, namespace):
    tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if getattr(n, "name", None) in names or
             isinstance(n, ast.Assign) and any(getattr(t, "id", None) in names for t in n.targets)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), namespace)
    return namespace


def render_fixture():
    env = dict(esc=html.escape, json=json, PWA_HEAD="", PWA_JS="",
               emotion=SimpleNamespace(STATES=("neutral", "determined", "happy"),
                                       DEFAULT_STATE="neutral", COLORS={}, get_state=lambda _: "determined"),
               accounts=SimpleNamespace(get_user=lambda _: {"display_name": "Test user"}),
               config=SimpleNamespace(get=lambda scope, uid, key: key == "show_tool_calls"),
               conversation=SimpleNamespace(recent=lambda *a, **kw: [
                   dict(id=1, role="assistant", content="Hello.", emotion="neutral", kind="chat"),
                   dict(id=2, role="assistant", content="Let's get started.", emotion="determined", kind="chat")]))
    definitions("server.py", {"BASE_CSS", "APP_JS", "CHAT_JS", "KEYBOARD_JS", "page_app"}, env)
    tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    handler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
    handler.bases = []
    handler.body = [n for n in handler.body if getattr(n, "name", None) in {
        "_msg_marker", "_hero_panel", "_peek_panel", "_hdr_menu", "_app_header", "_board_panel", "chat_page"}]
    exec(compile(ast.Module(body=[handler], type_ignores=[]), "server.py", "exec"), env)
    instance = env["Handler"]()
    instance.send = Mock()
    instance.chat_page(dict(user_id=1, role="member", csrf="fixture-token"))
    return instance.send.call_args.args[1]


class NoriToolVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, user_id INTEGER, role TEXT, content TEXT, kind TEXT)")
        self.env = definitions("conversation.py", {"VISIBLE_TOOLS_FILTER", "recent", "since"},
                               dict(DEFAULT_WINDOW=30, store=SimpleNamespace(read=lambda fn: fn(self.db))))

    def add(self, content, kind="tool", user_id=1):
        self.db.execute("INSERT INTO messages(user_id,role,content,kind) VALUES(?,'assistant',?,?)",
                        (user_id, content, kind))

    def test_history_hides_emotion_calls_before_limiting_without_hiding_other_tools(self):
        self.add("used remember")
        self.add("used set_emotion", kind="chat")
        for _ in range(35):
            self.add("used set_emotion")
        self.add("someone else's message", kind="chat", user_id=2)
        rows = self.env["recent"](1, limit=2, include_tool=True)
        self.assertEqual([r["content"] for r in rows], ["used remember", "used set_emotion"])
        self.assertEqual([r["kind"] for r in self.env["recent"](1)], ["chat"])

    def test_poll_skips_hidden_rows_without_starving_pagination(self):
        self.add("before cursor", kind="chat")
        for _ in range(205):
            self.add("used set_emotion")
        self.add("used remember")
        self.add("Ready.", kind="chat")
        self.add("private", kind="chat", user_id=2)
        rows = self.env["since"](1, 1, limit=2, include_tool=True)
        self.assertEqual([r["content"] for r in rows], ["used remember", "Ready."])
        self.assertEqual([r["content"] for r in self.env["since"](1, 1)], ["Ready."])

    def test_emotion_executes_and_is_logged_like_any_other_tool_call(self):
        for show_tools in (True, False):
            with self.subTest(show_tools=show_tools):
                messages = []
                calls = [{"id": str(i), "function": {"name": name, "arguments": "{}"}}
                         for i, name in enumerate(("set_emotion", "remember"))]
                call = Mock(side_effect=[dict(content="", tool_calls=calls), dict(content="Done.")])
                dispatch = Mock(return_value={"ok": True})
                add_message = Mock()
                env = definitions("chat.py", {"run", "_detect_leaked_call"}, dict(
                    json=json, re=re, MAX_TOOL_ROUNDS=3, call=call,
                    # run() resolves the workspace's own model/reasoning
                    # override via _resolve_model() and falls back to
                    # DEFAULT_MODEL -- neither is pulled in by extracting
                    # just "run" itself, so both need a stand-in here.
                    # (None, None) means "no override", same as a workspace
                    # that never set one.
                    _resolve_model=lambda workspace_id: (None, None),
                    DEFAULT_MODEL="x-ai/grok-4.3",
                    context=SimpleNamespace(build_messages=lambda *a, **kw: messages),
                    precheck=SimpleNamespace(build_block=lambda *a: None),
                    tools=SimpleNamespace(active_schemas=lambda _: [], dispatch=dispatch,
                                          is_consequential=lambda name: False),
                    config=SimpleNamespace(get=lambda *a: show_tools),
                    conversation=SimpleNamespace(add_message=add_message),
                    timing=SimpleNamespace(Turn=object, NULL_TURN=SimpleNamespace(
                        stage=lambda *a, **kw: _NullCtx(), finish=lambda: None))))
                # run() now reads session["workspace_id"] (to resolve the
                # workspace's own model override) -- every real caller
                # already passes a full session dict, so {} was never a
                # realistic session, just stale test data from before that
                # read existed.
                #
                # run() also returns {"text", "usage"} now, not a bare
                # string -- per its own docstring, that shape change
                # predates this test's last update and every real caller
                # was updated the same day the shape changed; this
                # assertion just never was.
                self.assertEqual(env["run"]({"workspace_id": 1}, 1, "Test")["text"], "Done.")
                self.assertEqual([c.args[0] for c in dispatch.call_args_list], ["set_emotion", "remember"])
                self.assertEqual(len([m for m in messages if m["role"] == "tool"]), 2)
                # set_emotion used to be excluded from this log at the
                # chat.py level entirely; a real, documented behaviour
                # change (2026-09-14, operator's own ask -- see chat.py's
                # own comment above this call site) made it log like any
                # other tool call, filtered from the chat VIEW only (see
                # VISIBLE_TOOLS_FILTER, covered by the two tests above this
                # one), not excluded from storage. Both calls are logged
                # here, gated only by show_tool_calls. meta (2026-09-19,
                # real gap found in the "ensure silence is truly her
                # choice" sweep) now always carries tool_name, structured,
                # so success/failure is queryable without parsing
                # `content` -- both calls here succeeded (dispatch's own
                # Mock returns {"ok": True}), so no `failed`/`error` key.
                if show_tools:
                    self.assertEqual(add_message.call_args_list, [
                        mock_call(1, "assistant", "used set_emotion", kind="tool",
                                 meta={"tool_name": "set_emotion"}),
                        mock_call(1, "assistant", "used remember", kind="tool",
                                 meta={"tool_name": "remember"})])
                else:
                    add_message.assert_not_called()

    def test_refused_peer_send_logs_failed_in_meta_not_content(self):
        # The actual bug this closes (2026-09-19): a refused peer_send
        # (cooldown, a cap, the rate limiter -- any tool error) used to
        # log identically to a successful one. Content stays "used
        # peer1_send" unchanged (server.py's toolRunLine() collapses
        # consecutive tool lines by string-splitting on "used ", so the
        # VISIBLE text must not change); the outcome lives in meta.
        messages = []
        calls = [{"id": "1", "function": {"name": "peer1_send", "arguments": "{}"}}]
        call = Mock(side_effect=[dict(content="", tool_calls=calls), dict(content="Done.")])
        dispatch = Mock(return_value={"error": "cooldown active for another 3 minute(s)"})
        add_message = Mock()
        env = definitions("chat.py", {"run", "_detect_leaked_call"}, dict(
            json=json, re=re, MAX_TOOL_ROUNDS=3, call=call,
            _resolve_model=lambda workspace_id: (None, None),
            DEFAULT_MODEL="x-ai/grok-4.3",
            context=SimpleNamespace(build_messages=lambda *a, **kw: messages),
            precheck=SimpleNamespace(build_block=lambda *a: None),
            tools=SimpleNamespace(active_schemas=lambda _: [], dispatch=dispatch,
                                  is_consequential=lambda name: False),
            config=SimpleNamespace(get=lambda *a: True),
            conversation=SimpleNamespace(add_message=add_message),
            timing=SimpleNamespace(Turn=object, NULL_TURN=SimpleNamespace(
                stage=lambda *a, **kw: _NullCtx(), finish=lambda: None))))
        env["run"]({"workspace_id": 1}, 1, "Test")
        add_message.assert_called_once_with(
            1, "assistant", "used peer1_send", kind="tool",
            meta={"tool_name": "peer1_send", "failed": True,
                 "error": "cooldown active for another 3 minute(s)"})


class RoundLimitTests(unittest.TestCase):
    """hit_round_limit (2026-09-19, real gap found investigating
    unexplained silence toward a peer): chat.run()'s own fallback text
    at round exhaustion used to be indistinguishable, by its caller,
    from a normal completion -- both returned a plain {"text", "usage"}
    dict. Exercises the REAL round loop (chat.call mocked, nothing else),
    not just the flag's presence in the source."""

    def _env(self, call, max_rounds):
        return definitions("chat.py", {"run", "_detect_leaked_call"}, dict(
            json=json, re=re, MAX_TOOL_ROUNDS=max_rounds, call=call,
            _resolve_model=lambda workspace_id: (None, None),
            DEFAULT_MODEL="x-ai/grok-4.3",
            context=SimpleNamespace(build_messages=lambda *a, **kw: []),
            precheck=SimpleNamespace(build_block=lambda *a: None),
            tools=SimpleNamespace(active_schemas=lambda _: [], dispatch=Mock(return_value={"ok": True}),
                                  is_consequential=lambda name: False),
            config=SimpleNamespace(get=lambda *a: False),
            conversation=SimpleNamespace(add_message=Mock()),
            timing=SimpleNamespace(Turn=object, NULL_TURN=SimpleNamespace(
                stage=lambda *a, **kw: _NullCtx(), finish=lambda: None))))

    def test_hit_round_limit_true_on_real_exhaustion(self):
        # Every round returns a real tool call, never plain text -- the
        # loop must run all `max_rounds` rounds and fall through to the
        # fixed fallback, never returning early.
        tool_call = [{"id": "1", "function": {"name": "set_emotion", "arguments": "{}"}}]
        call = Mock(side_effect=[dict(content="", tool_calls=tool_call) for _ in range(3)])
        env = self._env(call, max_rounds=3)
        res = env["run"]({"workspace_id": 1}, 1, "Test", max_rounds=3)
        self.assertTrue(res["hit_round_limit"])
        self.assertIn("hit the 3-step tool-call limit", res["text"])

    def test_hit_round_limit_false_on_normal_completion(self):
        call = Mock(return_value=dict(content="Done.", tool_calls=[]))
        env = self._env(call, max_rounds=3)
        res = env["run"]({"workspace_id": 1}, 1, "Test", max_rounds=3)
        self.assertFalse(res["hit_round_limit"])
        self.assertEqual(res["text"], "Done.")

    def test_hit_round_limit_false_when_model_finishes_on_the_last_round(self):
        # A real multi-step task that happens to wrap up exactly at the
        # cap (calls a tool on rounds 1-2, plain text on round 3) is a
        # normal completion, not exhaustion -- must not be flagged.
        tool_call = [{"id": "1", "function": {"name": "set_emotion", "arguments": "{}"}}]
        call = Mock(side_effect=[dict(content="", tool_calls=tool_call),
                                 dict(content="", tool_calls=tool_call),
                                 dict(content="Done, finally.", tool_calls=[])])
        env = self._env(call, max_rounds=3)
        res = env["run"]({"workspace_id": 1}, 1, "Test", max_rounds=3)
        self.assertFalse(res["hit_round_limit"])
        self.assertEqual(res["text"], "Done, finally.")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--fixture":
        Path(sys.argv[2]).write_bytes(render_fixture())
    else:
        unittest.main()

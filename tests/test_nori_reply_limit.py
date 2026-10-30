# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The reply-length ceiling and cut-off detection (2026-10-30, operator's own ask: Iron's replies were being truncated). The ceiling was a hard-coded 1024
tokens, and a reply that ran into it was stored cut off, mid-sentence, as if it were whole. Now: 4096 by default (or NORI_MAX_TOKENS), a workspace setting
the admin edits in Settings -> Model config, the real value passed to every turn's model call, and a provider's "I stopped because of length" (under any
API's name for it) turned into a visible note on the reply, a log line and a flag. Real chat.run() and config; only the model call is faked."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_reply_limit_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_reply_limit_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"
os.environ.pop("NORI_MAX_TOKENS", None)

import accounts  # noqa: E402
import chat  # noqa: E402
import config  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")
WS = _user["workspace_id"]
SESSION = {"user_id": _user["id"], "workspace_id": WS, "role": "admin", "csrf": "csrf-tok"}


def run_with(*rounds):
    seen = []

    def fake(messages, workspace_id, **kw):
        seen.append(kw)
        return rounds[min(len(seen), len(rounds)) - 1]
    with patch.object(chat, "call_via_chain", fake):
        out = chat.run(SESSION, _user["id"], _user["display_name"], max_rounds=3)
    return out, seen


class TheSetting(unittest.TestCase):
    def setUp(self):
        config.set("workspace", WS, "max_reply_tokens", config.spec()["max_reply_tokens"][0])

    def test_the_default_is_4096_not_1024(self):
        self.assertEqual(config.spec()["max_reply_tokens"][0], 4096)
        self.assertEqual(config.get("workspace", WS, "max_reply_tokens"), 4096)
        self.assertEqual(chat.DEFAULT_MAX_TOKENS, 4096)

    def test_it_is_a_workspace_setting_with_bounds(self):
        self.assertEqual(config.spec()["max_reply_tokens"][2], "workspace")
        with self.assertRaises(ValueError):
            config.set("workspace", WS, "max_reply_tokens", 100)
        with self.assertRaises(ValueError):
            config.set("workspace", WS, "max_reply_tokens", 10**6)
        config.set("workspace", WS, "max_reply_tokens", 8000)
        self.assertEqual(config.get("workspace", WS, "max_reply_tokens"), 8000)

    def test_the_environment_still_sets_the_default_when_it_is_sane(self):
        for raw, want in (("2048", 2048), ("", 4096), ("lots", 4096), ("10", 4096), ("999999", 4096), (" 6000 ", 6000)):
            with patch.dict(os.environ, {"NORI_MAX_TOKENS": raw}):
                self.assertEqual(config._env_int("NORI_MAX_TOKENS", 4096), want, raw)

    def test_the_live_value_reaches_the_model_call_each_turn(self):
        config.set("workspace", WS, "max_reply_tokens", 7777)
        _out, seen = run_with({"content": "hi", "tool_calls": [], "usage": {}, "finish_reason": "stop"})
        self.assertEqual(seen[0]["max_tokens"], 7777)
        config.set("workspace", WS, "max_reply_tokens", 1500)
        _out, seen = run_with({"content": "hi", "tool_calls": [], "usage": {}, "finish_reason": "stop"})
        self.assertEqual(seen[0]["max_tokens"], 1500, "read fresh each turn, so a change applies at once")


class Detection(unittest.TestCase):
    def setUp(self):
        config.set("workspace", WS, "max_reply_tokens", 4096)

    def test_every_providers_word_for_hitting_the_limit_counts_and_a_normal_stop_does_not(self):
        for reason in ("length", "max_tokens", "max_output_tokens", "incomplete", "LENGTH", " length "):
            self.assertTrue(chat.cut_off({"finish_reason": reason}), reason)
        for reason in ("stop", "end_turn", "tool_calls", "tool_use", "completed", "", None):
            self.assertFalse(chat.cut_off({"finish_reason": reason}), reason)
        self.assertFalse(chat.cut_off({}))
        self.assertFalse(chat.cut_off(None))

    def test_a_reply_that_hit_the_limit_says_so_and_keeps_what_there_is(self):
        out, _ = run_with({"content": "The first part of a long answer, and then it", "tool_calls": [], "usage": {}, "finish_reason": "length"})
        self.assertTrue(out["cut_off"])
        self.assertTrue(out["text"].startswith("The first part of a long answer, and then it"))
        self.assertIn("reply cut off at the 4096-token limit", out["text"])
        self.assertIn("Settings → Model config", out["text"])

    def test_the_note_names_the_limit_in_force(self):
        config.set("workspace", WS, "max_reply_tokens", 2000)
        out, _ = run_with({"content": "abc", "tool_calls": [], "usage": {}, "finish_reason": "max_tokens"})
        self.assertIn("2000-token limit", out["text"])

    def test_a_thinking_model_that_used_it_all_before_speaking_is_told_apart(self):
        out, _ = run_with({"content": "", "tool_calls": [], "usage": {}, "finish_reason": "length"})
        self.assertTrue(out["cut_off"])
        self.assertIn("used its whole 4096-token reply limit before saying anything", out["text"])
        self.assertNotIn("the model returned nothing", out["text"])

    def test_a_finished_reply_is_untouched(self):
        out, _ = run_with({"content": "All done.", "tool_calls": [], "usage": {}, "finish_reason": "stop"})
        self.assertFalse(out["cut_off"])
        self.assertEqual(out["text"], "All done.")

    def test_an_empty_reply_that_did_not_hit_the_limit_still_reads_as_nothing_returned(self):
        out, _ = run_with({"content": "", "tool_calls": [], "usage": {}, "finish_reason": "stop"})
        self.assertFalse(out["cut_off"])
        self.assertIn("the model returned nothing", out["text"])

    def test_the_cut_off_is_logged_with_the_reason(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_with({"content": "partial", "tool_calls": [], "usage": {}, "finish_reason": "length"})
        self.assertIn("REPLY CUT OFF at the 4096-token limit", buf.getvalue())
        self.assertIn("finish_reason='length'", buf.getvalue())

    def test_a_cut_off_tool_call_is_logged_too(self):
        import contextlib
        import io
        buf = io.StringIO()
        call = {"id": "1", "function": {"name": "no_such_tool", "arguments": "{\"a\":"}}
        with contextlib.redirect_stdout(buf):
            run_with({"content": "", "tool_calls": [call], "usage": {}, "finish_reason": "length"},
                     {"content": "recovered", "tool_calls": [], "usage": {}, "finish_reason": "stop"})
        self.assertIn("a tool call was in progress", buf.getvalue())

    def test_the_dispatch_meta_records_it_for_the_per_message_debug_panel(self):
        entry = {"alias": "Iron", "model_name": "some/model", "provider": {"type": "openrouter", "label": "OpenRouter"}, "reasoning_effort": None}
        for finish, expect in (("length", True), ("stop", False)):
            with patch.object(chat, "_resolve_model_chain", lambda ws: [entry]),                  patch.object(chat, "call_for_model", lambda e, m, **kw: {"content": "x", "usage": {}, "finish_reason": finish}):
                meta = chat.call_via_chain([], WS)["_dispatch_meta"]
            self.assertEqual((meta["finish_reason"], meta["cut_off"]), (finish, expect))


class TheAdminPage(unittest.TestCase):
    def setUp(self):
        config.set("workspace", WS, "max_reply_tokens", 4096)
        self.sent = {}
        h = server.Handler.__new__(server.Handler)
        h.send = lambda status, body, *a, **k: self.sent.update(status=status, body=body)
        h.forbidden = lambda: self.sent.update(status=403)
        h.headers = {}
        self.h = h

    def test_the_model_config_page_has_the_field_with_the_current_value(self):
        self.h.models_admin_form(SESSION)
        page = self.sent["body"].decode()
        self.assertIn("max reply length", page)
        self.assertIn("name=max_reply_tokens value='4096'", page)
        self.assertIn("/admin/models/reply-length", page)

    def test_saving_it(self):
        self.h.models_reply_length_post(SESSION, {"max_reply_tokens": "6000"})
        self.assertEqual(config.get("workspace", WS, "max_reply_tokens"), 6000)
        self.assertIn("saved", self.sent["body"].decode())
        self.assertIn("value='6000'", self.sent["body"].decode())

    def test_bad_values_are_refused_and_change_nothing(self):
        for bad in ("", "abc", "12.5", "100", "99999999"):
            self.h.models_reply_length_post(SESSION, {"max_reply_tokens": bad})
            self.assertIn("max reply length must be", self.sent["body"].decode(), bad)
            self.assertEqual(config.get("workspace", WS, "max_reply_tokens"), 4096, bad)

    def test_only_an_admin_can_change_it(self):
        self.h.models_reply_length_post(dict(SESSION, role="member"), {"max_reply_tokens": "9000"})
        self.assertEqual(self.sent["status"], 403)
        self.assertEqual(config.get("workspace", WS, "max_reply_tokens"), 4096)

    def test_the_post_route_is_wired(self):
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn('if path == "/admin/models/reply-length":', src)
        self.assertLess(src.index("csrf_ok(sess, form)"), src.index('if path == "/admin/models/reply-length":'), "behind the CSRF check")


if __name__ == "__main__":
    unittest.main()

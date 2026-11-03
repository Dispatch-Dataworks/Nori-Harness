# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""A Copilot model served only through /responses is learned once (2026-11-02, Sage: "reporting model errors but working correctly").
Every call to gpt-5.6-luna first tried /chat/completions, was refused with unsupported_api_for_model, and fell back; ModelError logs
itself on construction, so a healthy assistant logged two errors per call. Fakes only for the two endpoints."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_copilot_")
os.environ["NORI_NO_LOGFILE"] = "1"

import chat  # noqa: E402
import providers  # noqa: E402

ENTRY = {"provider": {"id": 1, "type": "github_copilot"}, "model_name": "gpt-5.6-luna", "reasoning_effort": "low"}
OK = {"content": "hi", "finish_reason": "completed", "tool_calls": [], "usage": {}, "request_id": "r1", "reasoning": None}


class ResponsesOnlyIsLearnedOnce(unittest.TestCase):
    def setUp(self):
        chat._COPILOT_RESPONSES_ONLY.clear()
        self.completions, self.responses = [], []

    def _completions(self, *a, **kw):
        self.completions.append(kw.get("model"))
        raise chat.ModelError('model call failed after 1 attempt(s): HTTP 400: {"error":{"code":"unsupported_api_for_model"}}')

    def _responses(self, entry, *a, **kw):
        self.responses.append(entry["model_name"])
        return OK

    def _run(self, n):
        out = io.StringIO()
        with patch.object(providers, "access_token_for", lambda _p: "tok"), patch.object(chat, "call", self._completions), \
                patch.object(chat, "_call_copilot_responses", self._responses), contextlib.redirect_stdout(out):
            results = [chat._call_github_copilot(ENTRY, [{"role": "user", "content": "hello"}]) for _ in range(n)]
        return results, out.getvalue()

    def test_only_the_first_call_is_refused(self):
        results, log = self._run(3)
        self.assertEqual([r["content"] for r in results], ["hi"] * 3)
        self.assertEqual(len(self.completions), 1, "the doomed /chat/completions request is made once, not on every call")
        self.assertEqual(len(self.responses), 3)
        self.assertEqual(log.count("chat.ModelError"), 1)
        self.assertIn("expected", log, "the one rejection that is logged says it is expected")

    def test_another_error_is_not_mistaken_for_it(self):
        def fail(*a, **kw):
            raise chat.ModelError("model call failed after 1 attempt(s): HTTP 401: bad token")
        with patch.object(providers, "access_token_for", lambda _p: "tok"), patch.object(chat, "call", fail), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(chat.ModelError):
                chat._call_github_copilot(ENTRY, [{"role": "user", "content": "hello"}])
        self.assertEqual(chat._COPILOT_RESPONSES_ONLY, set())

    def test_an_ordinary_model_keeps_using_chat_completions(self):
        with patch.object(providers, "access_token_for", lambda _p: "tok"), patch.object(chat, "call", lambda *a, **kw: OK), \
                patch.object(chat, "_call_copilot_responses", self._responses):
            for _ in range(2):
                chat._call_github_copilot({**ENTRY, "model_name": "gemini-4-argon"}, [{"role": "user", "content": "hello"}])
        self.assertEqual(self.responses, [])


if __name__ == "__main__":
    unittest.main()

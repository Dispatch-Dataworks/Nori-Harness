# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Responses-API tools must say "strict": false (2026-10-05).

The Responses API treats a missing `strict` as TRUE, and strict mode makes
every parameter required -- so on gpt-5.6-luna (OpenAI OAuth) tracker_log was
called with {"value_1": 187.2, "value_2": 0, "value_text": "", "note": "",
"ts": ""}, the backend rightly refused value_2 on a single-number tracker, and
43 of 45 attempts failed one evening with nothing logged. A live probe showed
the same call with strict:false sending only {"type_id": 1, "value_1": 187.2}.

These pin that every tool Nori sends over a Responses-style endpoint carries
the explicit flag, on the shared converter AND on the actual OpenAI OAuth
request body, and that the schema itself still marks only the truly required
fields required (so non-strict is the right mode for it)."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_resp_strict_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_resp_strict_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import chat  # noqa: E402
import providers  # noqa: E402
import store  # noqa: E402
import tools  # noqa: E402
import trackers  # noqa: E402,F401  (registers tracker_log)

store.init()

_CC_TOOL = {"type": "function", "function": {
    "name": "demo", "description": "d",
    "parameters": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "number"}},
                   "required": ["a"]}}}


class Converter(unittest.TestCase):
    def test_every_tool_is_explicitly_non_strict(self):
        out = chat._responses_tools([_CC_TOOL, tools.schema_for("tracker_log")])
        self.assertEqual(len(out), 2)
        for t in out:
            self.assertIs(t["strict"], False, t["name"])
            self.assertEqual(t["type"], "function")

    def test_shape_is_otherwise_unchanged(self):
        t = chat._responses_tools([_CC_TOOL])[0]
        self.assertEqual((t["name"], t["description"]), ("demo", "d"))
        self.assertEqual(t["parameters"], _CC_TOOL["function"]["parameters"])

    def test_no_tools_is_none(self):
        self.assertIsNone(chat._responses_tools(None))
        self.assertIsNone(chat._responses_tools([]))

    def test_tracker_log_schema_requires_only_type_id(self):
        # What makes non-strict correct for it: one required field, the rest optional.
        params = tools.schema_for("tracker_log")["function"]["parameters"]
        self.assertEqual(params["required"], ["type_id"])
        self.assertTrue({"value_1", "value_2", "value_text", "note", "ts"} <= set(params["properties"]))


class OpenAIOAuthRequest(unittest.TestCase):
    def test_request_body_sends_strict_false_on_every_tool(self):
        seen = {}

        def fake_run_call(fn, req, timeout, *, what):
            seen["body"] = json.loads(req.data)
            return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                    "usage": {}}

        entry = {"model_name": "gpt-x", "reasoning_effort": None,
                 "provider": {"account_meta": {"account_id": "acct"}, "type": "openai_oauth"}}
        with patch.object(chat, "_run_call", fake_run_call), \
             patch.object(providers, "access_token_for", return_value="tok"):
            try:
                chat._call_openai_oauth(entry, [{"role": "user", "content": "hi"}],
                                        tools=[_CC_TOOL, tools.schema_for("tracker_log")])
            except Exception as exc:  # noqa: BLE001 -- only the captured request body matters here
                if "body" not in seen:
                    self.skipTest(f"couldn't reach the request build without live credentials: {exc!r}")
        self.assertIn("body", seen)
        sent = seen["body"]["tools"]
        self.assertEqual([t["name"] for t in sent], ["demo", "tracker_log"])
        self.assertTrue(all(t["strict"] is False for t in sent))


if __name__ == "__main__":
    unittest.main()

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""read_file on an image (2026-11-03). Found live: vision was switched on,
but the vision model's OpenRouter account was out of credit, and read_file
answered "vision is off for this account", so she told the user to turn on
a setting that was already on and sent sub-agents that failed the same way.
A failed vision call must say it failed, and not be cached; vision off must
still say it's off.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_workfiles_vision_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_workfiles_vision_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import chat  # noqa: E402
import config  # noqa: E402
import ingest  # noqa: E402
import store  # noqa: E402
import workfiles  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")
_UID = _user["id"]
_SESSION = {"user_id": _UID, "workspace_id": _user["workspace_id"], "role": "admin", "csrf": "test-csrf"}

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _summarized(text, kind):
    return {"summary": text}


class ReadImageTests(unittest.TestCase):
    def setUp(self):
        assert workfiles.upload_file(_SESSION, f"{self._testMethodName}.png", PNG).get("ok")
        self.path = f"{self._testMethodName}.png"

    def tearDown(self):
        config.set("user", _UID, "workfile_vision_enabled", False)

    def test_vision_off_says_off_and_calls_nothing(self):
        config.set("user", _UID, "workfile_vision_enabled", False)
        with patch("chat.vision") as vis:
            r = workfiles.read_file(_SESSION, self.path)
        vis.assert_not_called()
        self.assertIn("vision is off", r["message"])

    def test_failed_vision_call_says_so_not_vision_off(self):
        config.set("user", _UID, "workfile_vision_enabled", True)
        err = chat.ModelError("model call failed after 1 attempt(s): HTTP 402: Insufficient credits")
        with patch("chat.vision", side_effect=err):
            r = workfiles.read_file(_SESSION, self.path)
        self.assertEqual(r["kind"], "image")
        self.assertNotIn("message", r)
        self.assertIn("402", r["error"])
        self.assertIn("Vision is switched on", r["error"])
        self.assertIn("Settings -> Images", r["error"])
        self.assertNotIn("vision is off", r["error"])

    def test_failure_is_not_cached_and_a_later_success_is(self):
        config.set("user", _UID, "workfile_vision_enabled", True)
        with patch("chat.vision", side_effect=chat.ModelError("HTTP 402")):
            workfiles.read_file(_SESSION, self.path)
        with patch("chat.vision", return_value="A grey square.") as vis, \
             patch.object(ingest, "summarize_untrusted", side_effect=_summarized):
            r = workfiles.read_file(_SESSION, self.path)
            again = workfiles.read_file(_SESSION, self.path)
        self.assertEqual(r["summary"], "A grey square.")
        self.assertEqual(again["summary"], "A grey square.")
        self.assertEqual(vis.call_count, 1)


if __name__ == "__main__":
    unittest.main()

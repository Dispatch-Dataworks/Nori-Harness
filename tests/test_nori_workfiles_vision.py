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


TRANSCRIPT = "WBC 18.2 x10^3/uL (High)\n" * 300     # longer than one page


def _screened(text, kind, preserve_content=False):
    return {"category": "informational", "suspicious": False, **({"content": text} if preserve_content else {"summary": text[:40]})}


class TranscriptionTests(unittest.TestCase):
    """Found live the same day: with vision working, a sub-agent granted exact file text still got a ~400-character gist of a few-sentence
    description, so MyChart values were lost, a "second pass" got the same cached text, and the job was marked "read as a gist only"."""

    def setUp(self):
        self.path = f"{self._testMethodName}.png"
        assert workfiles.upload_file(_SESSION, self.path, PNG).get("ok")
        config.set("user", _UID, "workfile_vision_enabled", True)
        self.addCleanup(config.set, "user", _UID, "workfile_vision_enabled", False)
        p = patch.object(ingest, "summarize_untrusted", side_effect=_screened)
        p.start()
        self.addCleanup(p.stop)

    def test_the_model_is_asked_to_transcribe_every_value(self):
        with patch("chat.vision", return_value="x") as vis:
            workfiles.read_file(_SESSION, self.path)
        prompt = vis.call_args.args[0]
        self.assertIn("every number with its unit", prompt)
        self.assertIn("never guess a value", prompt)
        self.assertEqual(vis.call_args.kwargs["max_tokens"], workfiles.VISION_MAX_TOKENS)

    def test_exact_access_gets_the_whole_transcription_paged(self):
        with patch("chat.vision", return_value=TRANSCRIPT):
            first = workfiles.read_file(_SESSION, self.path, preserve_content=True)
            self.assertEqual((first["kind"], first["offset"], first["total_chars"]), ("image", 0, len(TRANSCRIPT)))
            self.assertTrue(TRANSCRIPT.startswith(first["content"]))
            got, off = first["content"], first["next_offset"]
            while off is not None:
                page = workfiles.read_file(_SESSION, self.path, preserve_content=True, offset=off)
                got, off = got + page["content"], page["next_offset"]
        self.assertEqual(got, TRANSCRIPT)

    def test_a_sub_agent_reading_it_in_full_is_not_reported_as_gist_only(self):
        import jobs
        tracker = jobs._JobTracker([], raw_file_access=True)
        with patch("chat.vision", return_value="WBC 18.2"):
            tracker.saw_summary_read(self.path, workfiles.read_file(_SESSION, self.path))
            tracker.saw_raw_read(workfiles.read_file(_SESSION, self.path, preserve_content=True))
        self.assertEqual(tracker.problems(), [])

    def test_a_live_read_says_it_is_a_gist(self):
        with patch("chat.vision", return_value=TRANSCRIPT):
            r = workfiles.read_file(_SESSION, self.path)
        self.assertIn("read_file_full", r["note"])

    def test_an_older_description_is_read_again_and_a_replaced_picture_too(self):
        with patch("chat.vision", return_value="new reading") as vis:
            workfiles.read_file(_SESSION, self.path)
            store.write(lambda c: c.execute("UPDATE work_files SET vision_version=NULL WHERE user_id=? AND rel_path=?", (_UID, self.path)))
            workfiles.read_file(_SESSION, self.path)
            self.assertEqual(vis.call_count, 2, "a reading from the old prompt is not reused")
            workfiles.read_file(_SESSION, self.path)
            self.assertEqual(vis.call_count, 2, "the current reading is reused")
            assert workfiles.upload_file(_SESSION, self.path, PNG + b"\x01").get("ok")
            workfiles.read_file(_SESSION, self.path)
            self.assertEqual(vis.call_count, 3, "a new picture under the same name is read again")


if __name__ == "__main__":
    unittest.main()

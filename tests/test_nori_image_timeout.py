# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Image generation's timeout raise (60s -> 120s, 2026-09-17, real .nori.log
data) -- covers the two real risks flagged, not just the number:

1. A timeout must produce an HONEST, DISTINGUISHABLE tool result -- never
   a silent failure, never something that reads like a content refusal
   or generic error when it was actually just slow.
2. A long-running image call runs INSIDE the same per-user turn lock
   (turns.py) every other tool call does -- confirmed here with a real
   held lock, not just read off the code: while a (mocked-slow)
   generate_selfie_impl call is in flight, a second turns.run() for the
   SAME user correctly comes back {"queued": True} (never blocks, never
   drops) -- proving the existing compulsion-queue mechanism (built the
   same day, see test_nori_peer_compulsion.py) already covers this
   without needing its own special case.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_image_timeout_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import chat  # noqa: E402
import config  # noqa: E402
import imagegen  # noqa: E402
import store  # noqa: E402
import turns  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")
_SESSION = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"}
config.set("workspace", _user["workspace_id"], "image_gen_enabled", True)


class ImageTimeoutConstant(unittest.TestCase):
    def test_default_is_120_not_the_old_60(self):
        self.assertEqual(chat.IMAGE_TIMEOUT_S, 120)

    def test_env_override_respected(self):
        with patch.dict(os.environ, {"NORI_IMAGE_TIMEOUT_S": "45"}):
            import importlib
            reloaded = importlib.reload(chat)
            self.assertEqual(reloaded.IMAGE_TIMEOUT_S, 45)
        importlib.reload(chat)  # restore the real default for every later test


class HonestTimeoutMessage(unittest.TestCase):
    def test_openrouter_image_labels_a_real_timeout(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "not-a-real-key-just-for-this-test"}), \
             patch.object(chat.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError(TimeoutError("timed out"))):
            res = chat.openrouter_image("a prompt", model="bytedance-seed/seedream-5-0-lite")
        self.assertFalse(res["ok"])
        self.assertIn("timed out", res["reason"].lower())

    def test_clean_image_error_never_calls_a_timeout_a_content_refusal(self):
        msg, is_refusal = imagegen.clean_image_error("image API call timed out after 120s")
        self.assertFalse(is_refusal)
        self.assertIn("timed out", msg.lower())
        self.assertIn("120", msg)
        self.assertIn("don't say it worked", msg.lower())

    def test_generate_selfie_impl_surfaces_the_honest_timeout_reason(self):
        with patch.object(chat, "openrouter_image",
                          return_value={"ok": False, "reason": "image API call timed out after 120s"}):
            res = imagegen.generate_selfie_impl(_SESSION, "a test prompt")
        self.assertFalse(res["ok"])
        self.assertIn("timed out", res["reason"].lower())
        self.assertFalse(res["content_refusal"])


class ImageCallHoldsTheRealTurnLock(unittest.TestCase):
    def test_slow_image_call_keeps_the_lock_held_and_other_turns_queue_not_drop(self):
        release_gate = threading.Event()
        entered_call = threading.Event()

        def _slow_openrouter_image(prompt, *, model, seed=None, n=1, references=None):
            entered_call.set()
            release_gate.wait(timeout=5)
            return {"ok": True, "bytes": b"\x89PNG", "mime": "image/png", "cost": 0.035}

        result = {}

        def _first_run():
            with patch.object(chat, "openrouter_image", side_effect=_slow_openrouter_image):
                result["out"] = imagegen.generate_selfie_impl(_SESSION, "a test prompt")
            return {"ok": True}

        t = threading.Thread(target=lambda: turns.run(_user["id"], _first_run, lambda row: None))
        t.start()
        try:
            self.assertTrue(entered_call.wait(timeout=5), "the image call never started")
            # The lock is held right now, for the DURATION of the (still
            # sleeping) image call -- this is the exact property that was
            # asked to be confirmed, not assumed.
            self.assertTrue(turns.in_flight(_user["id"]))
            second = turns.run(_user["id"], lambda: {"ok": True}, lambda row: None)
            self.assertEqual(second, {"queued": True},
                            "a second turn for the same user during a slow image call must be "
                            "queued, never blocked and never silently dropped")
        finally:
            release_gate.set()
            t.join(timeout=5)
        self.assertFalse(turns.in_flight(_user["id"]))
        self.assertTrue(result["out"]["ok"])


if __name__ == "__main__":
    unittest.main()

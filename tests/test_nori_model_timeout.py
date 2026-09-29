# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""A real wall-clock ceiling on chat.call() (2026-09-25, found on a sibling
application's identical code: a single round against x-ai/grok-4.3 ran 1,707,344ms against
a CONFIGURED api_timeout_s of 120, with the app-wide turn lock held the
entire time -- urllib.request.urlopen(timeout=...) only bounds each
individual connect/read, never the call's total duration. Fixed in both
trees the same way: the actual urlopen runs on a worker thread, and the
deadline is imposed from the calling thread via Future.result(timeout=...),
so API_TIMEOUT_S now means total elapsed time. Tested against a genuinely
slow response, not just a fast one -- the failure path is the whole
feature."""
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_model_timeout_")
os.environ["NORI_NO_LOGFILE"] = "1"
os.environ.setdefault("OPENROUTER_API_KEY", "not-a-real-key-just-for-this-test")

import chat  # noqa: E402
import store  # noqa: E402

store.init()


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class TheWallClockCeiling(unittest.TestCase):
    def test_a_genuinely_slow_response_is_bounded_by_the_ceiling_not_the_actual_delay(self):
        real_delay_s = 0.6
        ceiling_s = 0.1

        def slow_urlopen(req, timeout=None):
            time.sleep(real_delay_s)   # a REAL sleep -- proves the caller doesn't wait for this to finish
            return _Resp({"choices": [{"message": {"content": "too late"}}]})

        with patch.object(chat.urllib.request, "urlopen", slow_urlopen):
            t0 = time.monotonic()
            with self.assertRaises(chat.ModelError) as cm:
                chat.call([{"role": "user", "content": "hi"}], timeout=ceiling_s)
            elapsed = time.monotonic() - t0

        self.assertLess(elapsed, real_delay_s, "the caller must not wait out the actual slow response")
        self.assertGreaterEqual(elapsed, ceiling_s * 0.8)
        e = cm.exception
        self.assertEqual(e.kind, "blocked_upstream")
        self.assertFalse(e.transient)
        self.assertIn(f"{ceiling_s}s", str(e))

    def test_the_ceiling_is_not_retried_one_slow_call_costs_one_ceiling_not_several(self):
        calls = []

        def slow_urlopen(req, timeout=None):
            calls.append(1)
            time.sleep(0.3)   # well past this test's 0.05s ceiling -- the submitted call is simply abandoned
            return _Resp({"choices": [{"message": {"content": "too late"}}]})

        with patch.object(chat.urllib.request, "urlopen", slow_urlopen):
            with self.assertRaises(chat.ModelError) as cm:
                chat.call([{"role": "user", "content": "hi"}], timeout=0.05)
        self.assertEqual(cm.exception.kind, "blocked_upstream")
        self.assertEqual(len(calls), 1, "a non-transient ceiling breach must not multiply the wait via retries")
        time.sleep(0.35)   # let the abandoned worker finish before the next test

    def test_a_fast_response_under_the_ceiling_is_unaffected(self):
        def fast_urlopen(req, timeout=None):
            return _Resp({"choices": [{"message": {"content": "hi back"}}]})

        with patch.object(chat.urllib.request, "urlopen", fast_urlopen):
            result = chat.call([{"role": "user", "content": "hi"}], timeout=5)
        self.assertEqual(result["content"], "hi back")

    def test_every_other_modelerror_still_has_no_kind_backward_compatible(self):
        with patch.object(chat.urllib.request, "urlopen",
                          side_effect=urllib.error.URLError("getaddrinfo failed")), \
             patch.object(chat.time, "sleep", lambda s: None):
            with self.assertRaises(chat.ModelError) as cm:
                chat.call([{"role": "user", "content": "hi"}], timeout=1)
        self.assertIsNone(cm.exception.kind)


if __name__ == "__main__":
    unittest.main()

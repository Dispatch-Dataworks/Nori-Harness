# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The xAI subscription proxy gates on a "Grok CLI" version label and
rejected the one Nori was sending ("Grok CLI is outdated: installed
0.2.101, required 1.0.13 or later", 2026-10-02). These pin the current
floor-clearing default, that every header that carries it agrees, and that
the next bump can be made from .env without a code change.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_xai_version_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_xai_version_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"
os.environ.pop("NORI_XAI_CLIENT_VERSION", None)

import providers  # noqa: E402

# The proxy's stated floor at the time of writing. A default below this is
# exactly the bug being fixed.
_FLOOR = (1, 0, 13)


def _tuple(v: str) -> tuple:
    return tuple(int(p) for p in v.split("."))


class DefaultVersion(unittest.TestCase):
    def test_default_clears_the_proxys_floor(self):
        self.assertGreaterEqual(_tuple(providers._XAI_CLIENT_VERSION), _FLOOR)

    def test_it_is_not_the_old_rejected_value(self):
        self.assertNotEqual(providers._XAI_CLIENT_VERSION, "0.2.101")

    def test_every_header_that_carries_it_agrees(self):
        v = providers._XAI_CLIENT_VERSION
        proxy = providers._xai_proxy_headers("grok-4.7")
        self.assertEqual(proxy["x-grok-client-version"], v)
        self.assertTrue(proxy["User-Agent"].startswith(f"grok-shell/{v} ("), proxy["User-Agent"])
        self.assertEqual(providers._XAI_DEVICE_HEADERS["x-grok-client-version"], v)

    def test_user_agent_carries_a_platform_label_like_a_native_client(self):
        ua = providers._xai_proxy_headers()["User-Agent"]
        self.assertRegex(ua, r"^grok-shell/\d+\.\d+\.\d+ \([a-z0-9_]+; [a-z0-9_]+\)$")

    def test_the_rest_of_the_proxy_identity_is_unchanged(self):
        h = providers._xai_proxy_headers("grok-4.7")
        self.assertEqual(h["x-grok-client-identifier"], "grok-shell")
        self.assertEqual(h["x-grok-client-mode"], "interactive")
        self.assertEqual(h["X-XAI-Token-Auth"], "xai-grok-cli")
        self.assertEqual(h["x-authenticateresponse"], "authenticate-response")
        self.assertEqual(h["x-grok-model-override"], "grok-4.7")
        self.assertNotIn("x-grok-model-override", providers._xai_proxy_headers())


class EnvOverride(unittest.TestCase):
    def _version_with_env(self, value):
        env = {**os.environ, "NORI_DATA_DIR": _SCRATCH}
        env.pop("NORI_XAI_CLIENT_VERSION", None)
        if value is not None:
            env["NORI_XAI_CLIENT_VERSION"] = value
        out = subprocess.run(
            [sys.executable, "-c", "import providers; print(providers._XAI_CLIENT_VERSION); "
             "print(providers._xai_proxy_headers()['User-Agent'])"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip().splitlines()

    def test_env_var_overrides_the_default_in_every_header(self):
        version, ua = self._version_with_env("9.8.7")
        self.assertEqual(version, "9.8.7")
        self.assertIn("grok-shell/9.8.7 (", ua)

    def test_blank_or_whitespace_falls_back_to_the_default(self):
        for blank in ("", "   "):
            self.assertEqual(self._version_with_env(blank)[0], providers._XAI_CLIENT_VERSION, repr(blank))

    def test_unset_uses_the_default(self):
        self.assertEqual(self._version_with_env(None)[0], providers._XAI_CLIENT_VERSION)


if __name__ == "__main__":
    unittest.main()

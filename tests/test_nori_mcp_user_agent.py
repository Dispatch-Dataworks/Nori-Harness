# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""MCP requests carry a named User-Agent (2026-11-10). urllib's default
"Python-urllib/3.x" is refused outright (HTTP 403, Cloudflare error 1010) by
many MCP servers behind Cloudflare; every _post() now sends
"Nori-Harness/<version>" instead. A server-specific header still wins."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_mcp_ua_")
os.environ["NORI_NO_LOGFILE"] = "1"

import mcp_client  # noqa: E402
import updates  # noqa: E402


class _Resp:
    headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"jsonrpc":"2.0","id":1,"result":{}}'


class UserAgent(unittest.TestCase):
    def _sent(self, headers=None):
        seen = {}

        def fake(req, timeout=None):
            seen.update({k.lower(): v for k, v in req.header_items()})
            return _Resp()
        with patch.object(mcp_client.urllib.request, "urlopen", fake):
            mcp_client._post("https://x.example/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                             session_id=None, headers=headers or {})
        return seen

    def test_sends_named_agent_with_version(self):
        self.assertEqual(self._sent()["user-agent"], f"Nori-Harness/{updates.current_version()}")

    def test_notification_also_sends_it(self):
        seen = {}

        def fake(req, timeout=None):
            seen.update({k.lower(): v for k, v in req.header_items()})
            return _Resp()
        with patch.object(mcp_client.urllib.request, "urlopen", fake):
            mcp_client._post("https://x.example/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"},
                             session_id=None, headers={})
        self.assertTrue(seen["user-agent"].startswith("Nori-Harness/"))

    def test_server_header_overrides(self):
        self.assertEqual(self._sent({"User-Agent": "custom/1"})["user-agent"], "custom/1")

    def test_other_headers_kept(self):
        seen = self._sent({"Authorization": "Bearer t"})
        self.assertEqual(seen["authorization"], "Bearer t")
        self.assertIn("text/event-stream", seen["accept"])

    def test_version_failure_does_not_break_requests(self):
        with patch.object(updates, "current_version", side_effect=RuntimeError("x")):
            self.assertEqual(mcp_client._user_agent(), "Nori-Harness")


if __name__ == "__main__":
    unittest.main()

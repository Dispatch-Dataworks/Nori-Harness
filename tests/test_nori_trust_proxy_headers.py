# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Handler.ip() (2026-09-26, pre-publication review finding): CF-Connecting-IP/
X-Forwarded-For are client-suppliable request headers, not connection facts --
trusting them unconditionally lets any direct caller spoof the IP the per-IP
login rate limiter keys on, defeating it outright. Covers both states of the
new NORI_TRUST_PROXY-backed switch directly against Handler.ip(), not a real
socket -- ip() only reads self.headers/self.client_address, so a bare object
duck-typing those two is the real function, not a stand-in for it.
"""
from __future__ import annotations

import email.message
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_trust_proxy_")

import server


def _fake_handler(headers: dict, client_ip: str = "203.0.113.9"):
    msg = email.message.Message()
    for k, v in headers.items():
        msg[k] = v
    return SimpleNamespace(headers=msg, client_address=(client_ip, 54321), ip=server.Handler.ip)


class TrustProxyHeadersOffByDefaultTests(unittest.TestCase):
    def setUp(self):
        self._orig = server.TRUST_PROXY_HEADERS
        server.TRUST_PROXY_HEADERS = False
        self.addCleanup(setattr, server, "TRUST_PROXY_HEADERS", self._orig)

    def test_spoofed_headers_are_ignored_real_peer_ip_used_instead(self):
        h = _fake_handler({"X-Forwarded-For": "1.2.3.4", "CF-Connecting-IP": "5.6.7.8"})
        self.assertEqual(h.ip(h), "203.0.113.9")

    def test_no_headers_at_all_still_uses_the_real_peer_ip(self):
        h = _fake_handler({})
        self.assertEqual(h.ip(h), "203.0.113.9")


class TrustProxyHeadersOnTests(unittest.TestCase):
    def setUp(self):
        self._orig = server.TRUST_PROXY_HEADERS
        server.TRUST_PROXY_HEADERS = True
        self.addCleanup(setattr, server, "TRUST_PROXY_HEADERS", self._orig)

    def test_cf_connecting_ip_wins_when_present(self):
        h = _fake_handler({"X-Forwarded-For": "1.2.3.4", "CF-Connecting-IP": "5.6.7.8"})
        self.assertEqual(h.ip(h), "5.6.7.8")

    def test_x_forwarded_for_used_first_hop_only_when_no_cf_header(self):
        h = _fake_handler({"X-Forwarded-For": "1.2.3.4, 9.9.9.9"})
        self.assertEqual(h.ip(h), "1.2.3.4")

    def test_falls_back_to_real_peer_ip_when_no_proxy_headers_present(self):
        h = _fake_handler({})
        self.assertEqual(h.ip(h), "203.0.113.9")


if __name__ == "__main__":
    unittest.main()

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The app icon follows the avatars (2026-10-29): the favicon, the apple-touch icon and the installed-app icons are drawn from the neutral avatar
(the avatar builder's untouched original if there is one), so installing a new avatar set changes them; with no avatar they stay the shipped static
files. Real server and Pillow, with the avatar folders pointed at a scratch directory so the repo's own avatars are never touched."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_icons_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_icons_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

from PIL import Image  # noqa: E402

import accounts  # noqa: E402
import imagegen  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

store.init()
accounts.bootstrap_admin("Tester", "testpass123")

_httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
_httpd.daemon_threads = True
BASE = f"http://127.0.0.1:{_httpd.server_address[1]}"
threading.Thread(target=_httpd.serve_forever, daemon=True).start()


def get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def _png(path: Path, colour, size=(300, 300)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path, "PNG")


def _pixel(data: bytes, xy=(10, 10)):
    with Image.open(io.BytesIO(data)) as im:
        return im.size, im.convert("RGB").getpixel(xy)


class Icons(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nori_test_icons_avatars_"))
        self.avatars = self.tmp / "avatars"
        self.avatars.mkdir()
        self.source = self.avatars / "source" / "neutral.png"
        for p in (patch.object(server, "AVATAR_DIR", self.avatars), patch.object(imagegen, "_REFERENCE_PATH", self.source)):
            p.start()
            self.addCleanup(p.stop)

    def test_with_no_avatar_the_shipped_icons_are_served_and_links_are_unversioned(self):
        status, body, _ = get("/icon-192.png")
        self.assertEqual(status, 200)
        self.assertEqual(body, (ROOT / "static" / "pwa" / "icon-192.png").read_bytes())
        self.assertEqual(server._icon_query(), "")
        status, page, _ = get("/login")
        self.assertIn(b"href='/icon-192.png'", page)

    def test_the_icons_are_drawn_from_the_installed_neutral_avatar_at_every_size(self):
        _png(self.avatars / "neutral.png", (200, 30, 30))
        for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("icon-180.png", 180)):
            status, body, headers = get("/" + name)
            self.assertEqual(status, 200, name)
            dims, (r, g, b) = _pixel(body)
            self.assertEqual(dims, (size, size), name)
            self.assertGreater(r, 150, name)
            self.assertLess(g, 80, name)
            self.assertEqual(headers["Cache-Control"], "no-cache", "revalidated, so a new avatar set shows up")

    def test_the_builders_untouched_original_wins_over_the_resized_avatar(self):
        _png(self.avatars / "neutral.png", (200, 30, 30))
        _png(self.source, (30, 30, 200), size=(1024, 1024))
        _dims, (r, _g, b) = _pixel(get("/icon-512.png")[1])
        self.assertGreater(b, 150)
        self.assertLess(r, 80)

    def test_a_new_avatar_set_changes_the_icon_the_links_and_the_manifest(self):
        _png(self.avatars / "neutral.png", (200, 30, 30))
        first = get("/icon-192.png")
        q1 = server._icon_query()
        self.assertTrue(q1.startswith("?v="))
        time.sleep(1.1)
        _png(self.avatars / "neutral.png", (30, 200, 30), size=(400, 400))
        second = get("/icon-192.png")
        self.assertNotEqual(first[1], second[1])
        self.assertGreater(_pixel(second[1])[1][1], 150)
        self.assertNotEqual(first[2]["ETag"], second[2]["ETag"])
        q2 = server._icon_query()
        self.assertNotEqual(q1, q2, "the link changes with the avatar, so a browser that cached the old favicon fetches the new one")
        self.assertIn(f"href='/icon-192.png{q2}'".encode(), get("/login")[1])
        icons = json.loads(get("/manifest.webmanifest")[1])["icons"]
        self.assertEqual([i["src"] for i in icons], ["/icon-192.png" + q2, "/icon-512.png" + q2])

    def test_a_matching_etag_gets_a_304(self):
        _png(self.avatars / "neutral.png", (200, 30, 30))
        etag = get("/icon-180.png")[2]["ETag"]
        status, body, _ = get("/icon-180.png", {"If-None-Match": etag})
        self.assertEqual((status, body), (304, b""))

    def test_a_non_square_avatar_is_cropped_not_squashed(self):
        im = Image.new("RGB", (600, 300), (255, 0, 0))
        im.paste((0, 0, 255), (150, 0, 450, 300))
        im.save(self.avatars / "neutral.png")
        dims, centre = _pixel(get("/icon-192.png")[1], (96, 96))
        self.assertEqual(dims, (192, 192))
        self.assertGreater(centre[2], 200)
        self.assertLess(_pixel(get("/icon-192.png")[1], (2, 96))[1][0], 50, "the sides are the middle square's blue, not the red margins")

    def test_an_unreadable_image_falls_back_to_the_shipped_icon(self):
        (self.avatars / "neutral.png").write_bytes(b"not an image")
        status, body, _ = get("/icon-192.png")
        self.assertEqual(status, 200)
        self.assertEqual(body, (ROOT / "static" / "pwa" / "icon-192.png").read_bytes())

    def test_an_svg_only_avatar_is_ignored(self):
        (self.avatars / "neutral.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
        self.assertIsNone(server._icon_source())


if __name__ == "__main__":
    unittest.main()

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""In-browser preview on the files page (2026-10-02, operator's own ask:
txt/md/yml/json etc., images and videos). Stored files are untrusted, so
the properties that matter are the SAFETY ones: text is escaped, never
served as markup; only bytes that really are a png/jpeg/gif/webp or a real
mp4/mov/webm/ogv are ever served inline, with the verified Content-Type
(never the filename's); everything else stays download-only. Range support
is tested too, since a <video> needs it to seek (and Safari to play).
Real store and filesystem; the Handler methods are called directly with
send() captured.
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_workfiles_preview_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_workfiles_preview_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402
import workfiles  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")
_UID = _user["id"]
_SESSION = {"user_id": _UID, "workspace_id": _user["workspace_id"], "role": "admin", "csrf": "test-csrf"}

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4  # 1 KB+ so ranges are meaningful
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
OGV = b"OggS" + b"\x00" * 64


def _put(name, data):
    assert workfiles.upload_file(_SESSION, name, data).get("ok")


class _Handler(server.Handler):
    """The real Handler, minus a socket: send() records what it was asked
    to send, headers is a plain dict."""
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.sent = []
        self.command = "GET"

    def send(self, code, body=b"", extra=None, *, ctype="text/html; charset=utf-8"):
        self.sent.append({"code": code, "body": body, "extra": extra or {}, "ctype": ctype})


def _raw(name, range_header=None):
    h = _Handler({"Range": range_header} if range_header else {})
    h.files_raw(_SESSION, name)
    return h.sent[0]


def _preview_html(name):
    h = _Handler()
    h.files_preview_page(_SESSION, name)
    return h.sent[0]["body"].decode()


class PreviewInfo(unittest.TestCase):
    def setUp(self):
        import shutil
        shutil.rmtree(workfiles.WORKFILES_DIR / str(_UID), ignore_errors=True)

    def test_kind_is_decided_from_the_real_bytes(self):
        for name, data, kind in [("a.png", PNG, "image"), ("a.jpg", JPG, "image"), ("a.gif", GIF, "image"),
                                 ("a.webp", WEBP, "image"), ("a.mp4", MP4, "video"), ("a.m4v", MP4, "video"),
                                 ("a.mov", MP4, "video"), ("a.webm", WEBM, "video"), ("a.ogv", OGV, "video"),
                                 ("a.md", b"# hi", "text"), ("a.yml", b"k: v\n", "text"),
                                 ("a.json", b'{"a":1}', "text"), ("a.txt", b"hello", "text")]:
            _put(name, data)
            self.assertEqual(workfiles.preview_info(_SESSION, name)["kind"], kind, name)

    def test_a_name_that_lies_about_its_bytes_is_binary_not_media(self):
        _put("fake.png", b"<html><script>alert(1)</script></html>")
        _put("fake.mp4", b"just some text pretending")
        for name in ("fake.png", "fake.mp4"):
            info = workfiles.preview_info(_SESSION, name)
            self.assertEqual(info["kind"], "binary", name)
            self.assertEqual(info["mime"], "application/octet-stream")

    def test_mime_comes_from_the_verified_bytes_not_the_extension(self):
        _put("really-a-jpeg.png", JPG)
        info = workfiles.preview_info(_SESSION, "really-a-jpeg.png")
        self.assertEqual((info["kind"], info["mime"]), ("image", "image/jpeg"))

    def test_unknown_extension_text_is_sniffed_and_binary_is_refused(self):
        _put("notes.weird", b"plain readable text\n")
        _put("blob.weird", b"\x00\x01\x02binary")
        self.assertEqual(workfiles.preview_info(_SESSION, "notes.weird")["kind"], "text")
        self.assertEqual(workfiles.preview_info(_SESSION, "blob.weird")["kind"], "binary")

    def test_names_for_the_listing(self):
        k = workfiles.preview_kind_for_name
        self.assertEqual([k("a.MD"), k("a.PNG"), k("a.MP4"), k("README"), k("Dockerfile"), k(".gitignore"),
                          k("a.zip"), k("a.exe"), k("noext")],
                         ["text", "image", "video", "text", "text", "text", None, None, None])

    def test_traversal_and_folders_are_errors(self):
        self.assertIn("error", workfiles.preview_info(_SESSION, "../../etc/passwd"))
        workfiles.create_folder(_SESSION, "dir")
        self.assertIn("error", workfiles.preview_info(_SESSION, "dir"))
        self.assertIn("error", workfiles.preview_info(_SESSION, "missing.txt"))

    def test_text_is_capped_and_says_so(self):
        _put("big.txt", b"x" * (workfiles.PREVIEW_TEXT_MAX_BYTES + 100))
        info = workfiles.preview_text(_SESSION, "big.txt")
        self.assertTrue(info["truncated"])
        self.assertEqual(len(info["text"]), workfiles.PREVIEW_TEXT_MAX_BYTES)
        _put("small.txt", b"tiny")
        self.assertFalse(workfiles.preview_text(_SESSION, "small.txt")["truncated"])

    def test_bad_utf8_and_a_bom_dont_break_text(self):
        _put("latin.txt", b"caf\xe9")
        self.assertIn("caf", workfiles.preview_text(_SESSION, "latin.txt")["text"])
        _put("bom.txt", b"\xef\xbb\xbfhello")
        self.assertEqual(workfiles.preview_text(_SESSION, "bom.txt")["text"], "hello")


class RawRoute(unittest.TestCase):
    def setUp(self):
        import shutil
        shutil.rmtree(workfiles.WORKFILES_DIR / str(_UID), ignore_errors=True)

    def test_verified_image_and_video_are_served_with_their_real_type_and_a_sandbox_csp(self):
        _put("p.png", PNG)
        _put("v.mp4", MP4)
        for name, mime in (("p.png", "image/png"), ("v.mp4", "video/mp4")):
            r = _raw(name)
            self.assertEqual((r["code"], r["ctype"]), (200, mime), name)
            self.assertEqual(r["extra"]["Content-Security-Policy"], "sandbox; default-src 'none'")
            self.assertEqual(r["extra"]["Accept-Ranges"], "bytes")

    def test_nothing_else_is_ever_served_inline(self):
        _put("a.txt", b"hello")
        _put("a.html", b"<script>alert(1)</script>")
        _put("a.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")
        _put("fake.png", b"<html><script>alert(1)</script></html>")
        _put("blob.bin", b"\x00\x01\x02")
        for name in ("a.txt", "a.html", "a.svg", "fake.png", "blob.bin", "missing.png", "../x"):
            self.assertEqual(_raw(name)["code"], 404, name)

    def test_range_requests(self):
        _put("v.mp4", MP4)
        size = len(MP4)
        r = _raw("v.mp4", "bytes=0-9")
        self.assertEqual((r["code"], r["body"]), (206, MP4[:10]))
        self.assertEqual(r["extra"]["Content-Range"], f"bytes 0-9/{size}")
        r = _raw("v.mp4", "bytes=100-")
        self.assertEqual((r["code"], r["body"]), (206, MP4[100:]))
        self.assertEqual(r["extra"]["Content-Range"], f"bytes 100-{size - 1}/{size}")
        r = _raw("v.mp4", "bytes=-16")
        self.assertEqual((r["code"], r["body"]), (206, MP4[-16:]))
        r = _raw("v.mp4", f"bytes=0-{size * 10}")  # end past EOF is clamped, not an error
        self.assertEqual((r["code"], len(r["body"])), (206, size))

    def test_unsatisfiable_and_malformed_ranges(self):
        _put("v.mp4", MP4)
        size = len(MP4)
        r = _raw("v.mp4", f"bytes={size}-")
        self.assertEqual(r["code"], 416)
        self.assertEqual(r["extra"]["Content-Range"], f"bytes */{size}")
        self.assertEqual(_raw("v.mp4", "bytes=-0")["code"], 416)
        self.assertEqual(_raw("v.mp4", "bytes=50-10")["code"], 416)
        # Garbage / multi-range fall back to the whole file rather than erroring.
        for junk in ("nonsense", "bytes=0-1,5-9", "items=0-1"):
            r = _raw("v.mp4", junk)
            self.assertEqual((r["code"], len(r["body"])), (200, size), junk)


class PreviewPage(unittest.TestCase):
    def setUp(self):
        import shutil
        shutil.rmtree(workfiles.WORKFILES_DIR / str(_UID), ignore_errors=True)

    def test_html_and_svg_source_is_escaped_never_rendered(self):
        _put("evil.html", b"<script>alert('xss')</script><img src=x onerror=alert(1)>")
        _put("evil.svg", b"<svg onload=alert(1)><script>alert(2)</script></svg>")
        for name in ("evil.html", "evil.svg"):
            page = _preview_html(name)
            self.assertNotIn("<script>alert", page, name)
            self.assertNotIn("<svg onload", page, name)
            self.assertIn("&lt;script&gt;", page, name)

    def test_markdown_yaml_json_show_their_content(self):
        _put("a.md", b"# Title\n\nsome *text*")
        _put("a.yml", b"key: value\nlist:\n  - one\n")
        _put("a.json", b'{"b":1,"a":[1,2]}')
        self.assertIn("# Title", _preview_html("a.md"))
        self.assertIn("key: value", _preview_html("a.yml"))
        pretty = _preview_html("a.json")
        self.assertIn("pretty-printed", pretty)
        self.assertIn('&quot;b&quot;: 1', pretty)

    def test_invalid_json_is_shown_as_is(self):
        _put("broken.json", b'{"a": ')
        page = _preview_html("broken.json")
        self.assertNotIn("pretty-printed", page)
        self.assertIn("{&quot;a&quot;: ", page)

    def test_image_and_video_pages_embed_through_the_raw_route(self):
        _put("pics/p.png", PNG)
        _put("v.webm", WEBM)
        page = _preview_html("pics/p.png")
        self.assertIn("<img src='/files/raw/pics/p.png'", page)
        self.assertIn("/files?path=pics", page)  # back to its folder
        self.assertIn("<video controls", _preview_html("v.webm"))

    def test_binary_gets_a_download_message_and_no_embed(self):
        _put("blob.bin", b"\x00\x01\x02")
        page = _preview_html("blob.bin")
        self.assertIn("no inline preview", page)
        self.assertNotIn("<video", page)
        self.assertNotIn("<img src='/files/raw", page)

    def test_truncation_notice(self):
        _put("big.log", b"line\n" * 200_000)
        self.assertIn("showing the first", _preview_html("big.log"))

    def test_a_missing_file_falls_back_to_the_folder_with_an_error(self):
        h = _Handler()
        h.files_preview_page(_SESSION, "nope.txt")
        self.assertIn("no such file", h.sent[0]["body"].decode())


class ListingLinks(unittest.TestCase):
    def setUp(self):
        import shutil
        shutil.rmtree(workfiles.WORKFILES_DIR / str(_UID), ignore_errors=True)

    def test_previewable_names_link_to_the_preview_and_others_do_not(self):
        _put("doc.md", b"x")
        _put("pic.png", PNG)
        _put("clip.mp4", MP4)
        _put("archive.zip", b"PK\x03\x04")
        h = _Handler()
        h.files_page(_SESSION, "")
        page = h.sent[0]["body"].decode()
        for name in ("doc.md", "pic.png", "clip.mp4"):
            self.assertIn(f"/files/preview?path={name}", page, name)
        self.assertNotIn("/files/preview?path=archive.zip", page)
        self.assertIn("/files/download/archive.zip", page)  # still downloadable


if __name__ == "__main__":
    unittest.main()

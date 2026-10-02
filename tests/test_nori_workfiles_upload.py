# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Multi-file / whole-folder upload on the files page (2026-10-02,
operator's own ask). The page's script sends one request per file to
files_upload_post in its JSON mode, with the file's path inside the chosen
folder as a separate `relpath` field. What matters: the folder structure
is recreated, every path segment still goes through workfiles' own name
validation (no escaping, no bad names), CSRF and session checks hold in
JSON mode, replacing an existing file is reported (not silent), and the
item limit counts the folders a bulk upload creates -- not just the files.
"""
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_workfiles_upload_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_workfiles_upload_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402
import workfiles  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")
_UID = _user["id"]
_TOKEN = accounts.new_session(_user, "127.0.0.1")
_SESS = accounts.get_session(_TOKEN)
_SESSION = {"user_id": _UID, "workspace_id": _user["workspace_id"], "role": "admin", "csrf": _SESS["csrf"]}
_BOUNDARY = "----noriTestBoundary"


def _multipart(fields: dict, filename: str | None, data: bytes) -> bytes:
    out = b""
    for k, v in fields.items():
        out += (f"--{_BOUNDARY}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    if filename is not None:
        out += (f"--{_BOUNDARY}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        out += data + b"\r\n"
    return out + f"--{_BOUNDARY}--\r\n".encode()


class _Handler(server.Handler):
    def __init__(self, body: bytes, *, json_mode=True, token=_TOKEN):
        self.headers = {"Content-Length": str(len(body)),
                        "Content-Type": f"multipart/form-data; boundary={_BOUNDARY}"}
        if json_mode:
            self.headers["X-Nori-Upload"] = "json"
        self.rfile = io.BytesIO(body)
        self._tok = token
        self.sent = []
        self.command = "POST"

    def token(self):
        return self._tok

    def send(self, code, body=b"", extra=None, *, ctype="text/html; charset=utf-8"):
        self.sent.append({"code": code, "body": body, "ctype": ctype})


def _upload(filename, data, *, relpath=None, path="", csrf=None, json_mode=True, token=_TOKEN):
    fields = {"csrf": csrf if csrf is not None else _SESSION["csrf"], "path": path}
    if relpath is not None:
        fields["relpath"] = relpath
    h = _Handler(_multipart(fields, filename, data), json_mode=json_mode, token=token)
    h.files_upload_post()
    r = h.sent[0]
    return r["code"], (json.loads(r["body"]) if r["ctype"] == "application/json" else r["body"].decode())


def _root():
    return workfiles.WORKFILES_DIR / str(_UID)


class FolderAndMultiUpload(unittest.TestCase):
    def setUp(self):
        import shutil
        shutil.rmtree(_root(), ignore_errors=True)
        store.write(lambda c: c.execute("DELETE FROM work_files WHERE user_id=?", (_UID,)))

    def test_relpath_recreates_the_folder_structure_under_the_current_folder(self):
        code, j = _upload("c.txt", b"deep", relpath="myproj/sub/c.txt", path="inbox")
        self.assertEqual((code, j["ok"], j["path"]), (200, True, "inbox/myproj/sub/c.txt"))
        self.assertEqual((_root() / "inbox/myproj/sub/c.txt").read_bytes(), b"deep")

    def test_a_batch_of_files_in_a_folder_lands_together(self):
        for rel in ("proj/a.md", "proj/b.md", "proj/img/x.png", "top.txt"):
            code, j = _upload(rel.rsplit("/", 1)[-1], rel.encode(), relpath=rel)
            self.assertTrue(j["ok"], (rel, j))
        for rel in ("proj/a.md", "proj/b.md", "proj/img/x.png", "top.txt"):
            self.assertEqual((_root() / rel).read_bytes(), rel.encode())

    def test_without_relpath_it_is_the_plain_single_file_upload(self):
        code, j = _upload("plain.txt", b"x", path="docs")
        self.assertEqual((j["ok"], j["path"]), (True, "docs/plain.txt"))

    def test_backslash_relpaths_from_windows_are_normalized(self):
        code, j = _upload("a.txt", b"x", relpath="folder\\inner\\a.txt")
        self.assertEqual(j["path"], "folder/inner/a.txt")

    def test_traversal_and_absolute_relpaths_are_refused_and_write_nothing(self):
        outside = _root().parent / "escaped.txt"
        for rel in ("../escaped.txt", "a/../../escaped.txt", "a/../../../escaped.txt"):
            code, j = _upload("e.txt", b"x", relpath=rel)
            self.assertFalse(j["ok"], rel)
        self.assertFalse(outside.exists())
        # An absolute-looking relpath is stripped to a path under the folder,
        # never written at the filesystem root.
        code, j = _upload("e.txt", b"x", relpath="/etc/passwd")
        self.assertEqual(j["path"], "etc/passwd")
        self.assertFalse(os.path.exists("/etc/nori_upload_test"))

    def test_invalid_names_in_any_segment_are_refused_per_file(self):
        for rel in ("ok/CON/a.txt", "ok/bad?name/a.txt", "ok/a.txt.", "ok/" + "x" * 250 + "/a.txt"):
            code, j = _upload("a.txt", b"x", relpath=rel)
            self.assertFalse(j["ok"], rel)
            self.assertTrue(j["error"])

    def test_replacing_an_existing_file_is_reported(self):
        _, first = _upload("r.txt", b"one", relpath="d/r.txt")
        _, second = _upload("r.txt", b"two", relpath="d/r.txt")
        self.assertFalse(first["replaced"])
        self.assertTrue(second["replaced"])
        self.assertEqual((_root() / "d/r.txt").read_bytes(), b"two")

    def test_work_files_metadata_is_recorded_as_a_user_upload(self):
        _upload("m.txt", b"x", relpath="f/m.txt")
        row = store.read(lambda c: c.execute(
            "SELECT created_by FROM work_files WHERE user_id=? AND rel_path=?", (_UID, "f/m.txt")).fetchone())
        self.assertEqual(row["created_by"], "user")


class JsonModeSecurity(unittest.TestCase):
    def setUp(self):
        import shutil
        shutil.rmtree(_root(), ignore_errors=True)

    def test_bad_csrf_is_a_403_json_and_writes_nothing(self):
        code, j = _upload("a.txt", b"x", relpath="a.txt", csrf="wrong")
        self.assertEqual((code, j["ok"]), (403, False))
        self.assertFalse((_root() / "a.txt").exists())

    def test_no_session_is_a_401_json_not_a_redirect(self):
        code, j = _upload("a.txt", b"x", relpath="a.txt", token="not-a-real-token")
        self.assertEqual((code, j["ok"]), (401, False))

    def test_missing_file_part_is_a_json_error(self):
        h = _Handler(_multipart({"csrf": _SESSION["csrf"], "path": ""}, None, b""))
        h.files_upload_post()
        self.assertFalse(json.loads(h.sent[0]["body"])["ok"])

    def test_oversize_body_is_refused_with_a_json_error(self):
        big = b"x" * int((workfiles.MAX_FILE_MB + 3) * 1024 * 1024)
        code, j = _upload("big.bin", big, relpath="big.bin")
        self.assertFalse(j["ok"])
        self.assertIn("too large", j["error"])
        self.assertFalse((_root() / "big.bin").exists())

    def test_non_json_mode_still_returns_the_rendered_page(self):
        code, body = _upload("p.txt", b"x", json_mode=False)
        self.assertEqual(code, 200)
        self.assertIn("uploaded p.txt", body)


class ItemLimitCountsFolders(unittest.TestCase):
    def setUp(self):
        import shutil
        shutil.rmtree(_root(), ignore_errors=True)
        self._orig = workfiles.MAX_USER_COUNT

    def tearDown(self):
        workfiles.MAX_USER_COUNT = self._orig

    def test_new_parent_folders_count_toward_the_item_limit(self):
        # limit 4 = 1 file + 3 new folders fits exactly; a second file in a
        # DIFFERENT new 3-deep folder must not.
        workfiles.MAX_USER_COUNT = 4
        _, ok = _upload("a.txt", b"x", relpath="p/q/r/a.txt")
        self.assertTrue(ok["ok"], ok)
        _, over = _upload("b.txt", b"x", relpath="s/t/u/b.txt")
        self.assertFalse(over["ok"])
        self.assertIn("item limit", over["error"])
        self.assertFalse((_root() / "s").exists())

    def test_a_file_into_an_existing_folder_only_costs_one(self):
        workfiles.MAX_USER_COUNT = 3
        _upload("a.txt", b"x", relpath="d/a.txt")                  # d + a = 2
        self.assertTrue(_upload("b.txt", b"x", relpath="d/b.txt")[1]["ok"])  # +1 = 3
        self.assertFalse(_upload("c.txt", b"x", relpath="d/c.txt")[1]["ok"])  # would be 4


class PageWiring(unittest.TestCase):
    def test_files_page_has_multi_select_folder_button_message_area_and_script(self):
        h = _Handler(b"")
        h.files_page(_SESSION, "")
        page = h.sent[0]["body"].decode()
        self.assertIn("id=upfiles type=file name=file multiple", page)
        self.assertIn("id=upfolder type=file webkitdirectory", page)
        self.assertIn("id=upfolderbtn", page)
        self.assertIn("id=upmsg", page)
        self.assertIn("noriUploadAll", page)
        self.assertIn(f'"maxFileBytes": {int(workfiles.MAX_FILE_MB * 1024 * 1024)}', page)
        self.assertNotIn("__CFG__", page)

    def test_script_config_cannot_break_out_of_the_script_tag(self):
        workfiles.create_folder(_SESSION, "evil")
        h = _Handler(b"")
        h.files_page(_SESSION, "evil")
        page = h.sent[0]["body"].decode()
        cfg = page[page.index("var cfg=") + 8: page.index(";\nvar SKIP")]
        self.assertNotIn("</", cfg)


if __name__ == "__main__":
    unittest.main()

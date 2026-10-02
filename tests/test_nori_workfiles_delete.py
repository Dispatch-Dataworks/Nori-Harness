# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Recursive folder delete on the files page (2026-10-02, operator's own
ask). The properties that matter: it's a human-only act (no model-facing
tool can reach it), it can never take the working-folder root with it, it
cleans up work_files metadata for exactly the deleted tree (no LIKE-
wildcard over-deletion of neighbors), and the page's confirm says what's
about to go. Real store and real filesystem under a scratch data dir.
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_workfiles_delete_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_workfiles_delete_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402
import workfiles  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")
_UID = _user["id"]
_SESSION = {"user_id": _UID, "workspace_id": _user["workspace_id"], "role": "admin", "csrf": "test-csrf"}


def _root():
    return workfiles.WORKFILES_DIR / str(_UID)


def _meta_paths():
    rows = store.read(lambda c: c.execute(
        "SELECT rel_path FROM work_files WHERE user_id=?", (_UID,)).fetchall())
    return {r["rel_path"] for r in rows}


def _tree():
    """proj/ with a user-placed file, a Nori-created file, a nested subfolder
    holding another file, and an empty subfolder."""
    workfiles.upload_file(_SESSION, "proj/placed.md", b"user file")
    workfiles.write_file(_SESSION, "proj/nori.md", "nori file")
    workfiles.write_file(_SESSION, "proj/deep/er/nested.md", "nested")
    workfiles.create_folder(_SESSION, "proj/empty")


class RecursiveDelete(unittest.TestCase):
    def setUp(self):
        import shutil
        shutil.rmtree(_root(), ignore_errors=True)
        store.write(lambda c: c.execute("DELETE FROM work_files WHERE user_id=?", (_UID,)))

    def test_default_delete_of_a_non_empty_folder_still_refuses(self):
        _tree()
        result = workfiles.delete_file_ui(_SESSION, "proj")
        self.assertIn("isn't empty", result["error"])
        self.assertTrue((_root() / "proj/placed.md").exists())

    def test_recursive_ui_delete_removes_the_whole_tree_and_reports_counts(self):
        _tree()
        result = workfiles.delete_file_ui(_SESSION, "proj", recursive=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["files"], result["folders"]), (3, 3))  # deep, er, empty
        self.assertFalse((_root() / "proj").exists())

    def test_it_removes_user_placed_files_too_since_a_human_asked(self):
        _tree()
        workfiles.delete_file_ui(_SESSION, "proj", recursive=True)
        self.assertFalse((_root() / "proj/placed.md").exists())

    def test_metadata_rows_for_the_tree_are_cleaned_up(self):
        _tree()
        self.assertTrue(any(p.startswith("proj/") for p in _meta_paths()))
        workfiles.delete_file_ui(_SESSION, "proj", recursive=True)
        self.assertFalse(any(p.startswith("proj/") for p in _meta_paths()))

    def test_neighbors_with_similar_names_and_like_wildcards_are_untouched(self):
        # "proj_x" / "proj2" would match a naive `LIKE 'proj_%'` and a name
        # with a literal % or _ would act as a wildcard -- none may go.
        workfiles.write_file(_SESSION, "proj/a.md", "a")
        workfiles.write_file(_SESSION, "proj2/b.md", "b")
        workfiles.write_file(_SESSION, "proj_x/c.md", "c")
        workfiles.write_file(_SESSION, "projectile.md", "d")
        workfiles.delete_file_ui(_SESSION, "proj", recursive=True)
        self.assertEqual(_meta_paths(), {"proj2/b.md", "proj_x/c.md", "projectile.md"})
        for rel in ("proj2/b.md", "proj_x/c.md", "projectile.md"):
            self.assertTrue((_root() / rel).exists(), rel)

    def test_a_folder_named_with_a_percent_deletes_only_itself(self):
        workfiles.write_file(_SESSION, "100%/a.md", "a")
        workfiles.write_file(_SESSION, "1000/b.md", "b")
        workfiles.delete_file_ui(_SESSION, "100%", recursive=True)
        self.assertEqual(_meta_paths(), {"1000/b.md"})

    def test_model_facing_delete_cannot_remove_a_non_empty_folder(self):
        _tree()
        self.assertIn("error", workfiles.delete_file(_SESSION, "proj"))
        # Even asked for directly, recursive without human_action is refused.
        self.assertIn("error", workfiles._delete(_UID, "proj", recursive=True))
        self.assertTrue((_root() / "proj/placed.md").exists())

    def test_the_delete_tool_schema_exposes_no_recursive_option(self):
        import tools
        props = tools.schema_for("delete_file")["function"]["parameters"]["properties"]
        self.assertEqual(list(props), ["path"])

    def test_the_working_folder_root_can_never_be_deleted(self):
        _tree()
        for path in ("", "/", "."):
            result = workfiles.delete_file_ui(_SESSION, path, recursive=True)
            self.assertIn("error", result, path)
        self.assertTrue((_root() / "proj/placed.md").exists())
        # ...including when it's empty (the old rmdir path).
        import shutil
        shutil.rmtree(_root())
        self.assertIn("error", workfiles.delete_file_ui(_SESSION, ""))
        self.assertTrue(_root().exists())

    def test_traversal_out_of_the_working_folder_is_refused(self):
        outside = _root().parent / "outside-victim"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "keep.txt").write_text("keep")
        result = workfiles.delete_file_ui(_SESSION, "../outside-victim", recursive=True)
        self.assertIn("error", result)
        self.assertTrue((outside / "keep.txt").exists())

    def test_empty_folder_and_plain_file_deletes_are_unchanged(self):
        workfiles.create_folder(_SESSION, "emptydir")
        workfiles.write_file(_SESSION, "f.md", "x")
        self.assertTrue(workfiles.delete_file_ui(_SESSION, "emptydir", recursive=True)["ok"])
        self.assertTrue(workfiles.delete_file_ui(_SESSION, "f.md", recursive=True)["ok"])
        self.assertFalse((_root() / "emptydir").exists())
        self.assertFalse((_root() / "f.md").exists())

    def test_folder_stats_counts_files_folders_and_bytes(self):
        _tree()
        st = workfiles.folder_stats(_SESSION, "proj")
        self.assertEqual((st["files"], st["folders"]), (3, 3))
        self.assertEqual(st["bytes"], len(b"user file") + len(b"nori file") + len(b"nested"))


class FilesPage(unittest.TestCase):
    """The page itself, rendered and posted through the real Handler
    methods -- the confirm text and the recursive flag are what make this
    safe to expose, so they're asserted on the real HTML."""

    def setUp(self):
        import shutil
        shutil.rmtree(_root(), ignore_errors=True)
        store.write(lambda c: c.execute("DELETE FROM work_files WHERE user_id=?", (_UID,)))

    def _page(self, path=""):
        h = server.Handler.__new__(server.Handler)
        out = []
        h.send = lambda code, body, headers=None, **kw: out.append((code, body))
        h.files_page(_SESSION, path)
        return out[0][1].decode()

    def _post_delete(self, rel, **extra):
        h = server.Handler.__new__(server.Handler)
        out = []
        h.send = lambda code, body, headers=None, **kw: out.append((code, body))
        h.files_delete_post(_SESSION, {"path": rel, **extra})
        return out[0][1].decode()

    def test_non_empty_folder_row_carries_a_confirm_that_states_what_is_inside(self):
        _tree()
        html_out = self._page()
        self.assertIn("EVERYTHING inside it", html_out)
        self.assertIn("3 file(s), 3 subfolder(s)", html_out)
        self.assertIn("name=recursive value=1", html_out)

    def test_empty_folder_row_has_no_confirm(self):
        workfiles.create_folder(_SESSION, "emptydir")
        html_out = self._page()
        self.assertNotIn("EVERYTHING inside it", html_out)
        self.assertIn("folder · empty", html_out)

    def test_post_with_recursive_deletes_and_reports_it(self):
        _tree()
        body = self._post_delete("proj", recursive="1")
        self.assertIn("deleted the folder and everything in it", body)
        self.assertFalse((_root() / "proj").exists())

    def test_post_without_recursive_is_still_refused(self):
        _tree()
        body = self._post_delete("proj")
        self.assertIn("isn&#x27;t empty", body)
        self.assertTrue((_root() / "proj/placed.md").exists())


if __name__ == "__main__":
    unittest.main()

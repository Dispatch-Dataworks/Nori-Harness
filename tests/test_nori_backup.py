# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Daily backups (2026-09-18): real create->encrypt->restore round trip
against a throwaway data dir (not mocked -- this is the one place a
mock would hide a real corruption bug), plus mocked-Graph/Drive tests
for the remote upload/prune paths that can't be exercised live (no
Microsoft/Google account has ever been connected on this instance)."""
import datetime
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_backup_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_NO_LOGFILE"] = "1"
os.environ["NORI_BACKUP_KEY"] = Fernet.generate_key().decode("ascii")
os.environ["NORI_BACKUP_EXCLUDE_DIRS"] = "legacy-snapshot"

import accounts  # noqa: E402
import backup  # noqa: E402
import connected_accounts  # noqa: E402
import crypto  # noqa: E402
import store  # noqa: E402


def _ok(data):
    return {"ok": True, "data": data}


class ExcludeDirNamesTests(unittest.TestCase):
    """_exclude_dir_names() itself, isolated from the rest of the backup
    machinery: "backups" always excludes regardless of the env var, and
    NORI_BACKUP_EXCLUDE_DIRS is read fresh on every call, not frozen at
    import (same reasoning store.DATA_DIR's own history already
    established)."""

    def setUp(self):
        self._orig = os.environ.get("NORI_BACKUP_EXCLUDE_DIRS")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._orig is None:
            os.environ.pop("NORI_BACKUP_EXCLUDE_DIRS", None)
        else:
            os.environ["NORI_BACKUP_EXCLUDE_DIRS"] = self._orig

    def test_default_excludes_only_backups_itself(self):
        os.environ.pop("NORI_BACKUP_EXCLUDE_DIRS", None)
        self.assertEqual(backup._exclude_dir_names(), {"backups"})

    def test_configured_names_are_added_comma_separated(self):
        os.environ["NORI_BACKUP_EXCLUDE_DIRS"] = "foo, bar ,, baz"
        self.assertEqual(backup._exclude_dir_names(), {"backups", "foo", "bar", "baz"})

    def test_reads_fresh_each_call_not_frozen_at_import(self):
        os.environ["NORI_BACKUP_EXCLUDE_DIRS"] = "first"
        self.assertIn("first", backup._exclude_dir_names())
        os.environ["NORI_BACKUP_EXCLUDE_DIRS"] = "second"
        names = backup._exclude_dir_names()
        self.assertIn("second", names)
        self.assertNotIn("first", names)


class RealRoundTripTests(unittest.TestCase):
    """No mocks -- real files, real encryption, real restore_backup.py
    subprocess, real sqlite integrity check. This is what actually proves
    a backup is restorable, not an assumption about the code."""

    @classmethod
    def setUpClass(cls):
        store.init()
        cls.user = accounts.bootstrap_admin("Tester", "testpass123")
        crypto.encrypt("trigger secret.key creation")
        (store.DATA_DIR / "generated").mkdir(parents=True, exist_ok=True)
        (store.DATA_DIR / "generated" / "avatar1.png").write_bytes(b"fake generated image bytes")
        (store.DATA_DIR / "prompts").mkdir(parents=True, exist_ok=True)
        (store.DATA_DIR / "prompts" / "persona.md").write_text("=== PROMPT STARTS ===\nreal\n=== PROMPT ENDS ===")
        wf = store.DATA_DIR / "workfiles" / "1"
        wf.mkdir(parents=True, exist_ok=True)
        (wf / "real_user_file.png").write_bytes(b"a real file the user put there")
        derived = wf / "legacy-snapshot"
        derived.mkdir(parents=True, exist_ok=True)
        (derived / "server.py").write_text("# a stale, large, regenerable derived artifact -- not real backup-worthy data")

    def test_create_backup_produces_an_encrypted_local_file(self):
        rec = backup.create_backup(app="nori")
        self.assertEqual(rec["status"], "ok", rec)
        self.assertTrue(Path(rec["path"]).is_file())
        raw = Path(rec["path"]).read_bytes()
        with self.assertRaises(Exception):
            # not valid gzip/tar without decrypting first -- proves this
            # isn't sitting on disk as a plain tarball
            import tarfile
            tarfile.open(fileobj=__import__("io").BytesIO(raw))

    def test_restore_round_trip_via_the_real_cli_script(self):
        rec = backup.create_backup(app="nori")
        self.assertEqual(rec["status"], "ok")
        restore_target = Path(tempfile.mkdtemp(prefix="nori_test_backup_restore_"))
        self.addCleanup(shutil.rmtree, restore_target, ignore_errors=True)
        result = subprocess.run(
            [sys.executable, str(ROOT / "restore_backup.py"), rec["path"],
             "--target", str(restore_target), "--yes"],
            capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((restore_target / "nori.db").is_file())
        self.assertEqual((restore_target / "generated" / "avatar1.png").read_bytes(),
                         b"fake generated image bytes")
        self.assertTrue((restore_target / "workfiles" / "1" / "real_user_file.png").is_file())
        self.assertTrue((restore_target / "secret.key").is_file())
        self.assertFalse((restore_target / "workfiles" / "1" / "legacy-snapshot").exists(),
                         "a folder named in NORI_BACKUP_EXCLUDE_DIRS must be excluded")
        import sqlite3
        conn = sqlite3.connect(str(restore_target / "nori.db"))
        row = conn.execute("SELECT display_name, role FROM users").fetchone()
        conn.close()
        self.assertEqual(row, ("Tester", "admin"))

    def test_wrong_key_refuses_cleanly_no_partial_extraction(self):
        rec = backup.create_backup(app="nori")
        bad_target = Path(tempfile.mkdtemp(prefix="nori_test_backup_badkey_"))
        self.addCleanup(shutil.rmtree, bad_target, ignore_errors=True)
        env = dict(os.environ)
        env["NORI_BACKUP_KEY"] = Fernet.generate_key().decode("ascii")  # a DIFFERENT key
        result = subprocess.run(
            [sys.executable, str(ROOT / "restore_backup.py"), rec["path"],
             "--target", str(bad_target), "--yes"],
            capture_output=True, text=True, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("couldn't decrypt", result.stderr)
        self.assertEqual(list(bad_target.iterdir()), [])

    def test_no_key_configured_refuses_to_write_unencrypted(self):
        with patch.object(backup, "_backup_secret", return_value=None):
            rec = backup.create_backup(app="nori")
        self.assertEqual(rec["status"], "error")
        self.assertIn("NORI_BACKUP_KEY", rec["error"])

    def test_passphrase_mode_round_trip_via_the_real_cli_script(self):
        """The documented default path (2026-09-19 revision): a human
        passphrase, not a generated key -- real encrypt with create_backup(),
        real restore via the actual restore_backup.py subprocess, using
        ONLY the passphrase (the scrypt salt travels inside the archive
        itself, nothing else needed)."""
        passphrase = "correct horse battery staple zebra"  # 5 words, 35 chars
        old_key = os.environ["NORI_BACKUP_KEY"]
        os.environ["NORI_BACKUP_KEY"] = passphrase
        try:
            rec = backup.create_backup(app="nori")
        finally:
            os.environ["NORI_BACKUP_KEY"] = old_key
        self.assertEqual(rec["status"], "ok", rec)
        raw = Path(rec["path"]).read_bytes()
        self.assertTrue(raw.startswith(backup._ARCHIVE_MAGIC))
        self.assertEqual(raw[len(backup._ARCHIVE_MAGIC)], backup._MODE_PASSPHRASE)

        restore_target = Path(tempfile.mkdtemp(prefix="nori_test_backup_restore_pass_"))
        self.addCleanup(shutil.rmtree, restore_target, ignore_errors=True)
        env = dict(os.environ)
        env["NORI_BACKUP_KEY"] = passphrase
        result = subprocess.run(
            [sys.executable, str(ROOT / "restore_backup.py"), rec["path"],
             "--target", str(restore_target), "--yes"],
            capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((restore_target / "nori.db").is_file())
        self.assertEqual((restore_target / "generated" / "avatar1.png").read_bytes(),
                         b"fake generated image bytes")
        self.assertTrue((restore_target / "workfiles" / "1" / "real_user_file.png").is_file())
        self.assertTrue((restore_target / "secret.key").is_file())
        self.assertFalse((restore_target / "workfiles" / "1" / "legacy-snapshot").exists())
        import sqlite3
        conn = sqlite3.connect(str(restore_target / "nori.db"))
        row = conn.execute("SELECT display_name, role FROM users").fetchone()
        conn.close()
        self.assertEqual(row, ("Tester", "admin"))

    def test_passphrase_too_short_refuses_loudly_before_any_real_work(self):
        old_key = os.environ["NORI_BACKUP_KEY"]
        os.environ["NORI_BACKUP_KEY"] = "short"  # not a valid raw key, well under MIN_PASSPHRASE_LEN
        try:
            rec = backup.create_backup(app="nori")
        finally:
            os.environ["NORI_BACKUP_KEY"] = old_key
        self.assertEqual(rec["status"], "error")
        self.assertIn("characters", rec["error"])
        self.assertNotIn("path", rec)  # never got as far as writing a file

    def test_wrong_passphrase_refuses_cleanly_no_partial_extraction(self):
        passphrase = "correct horse battery staple zebra"
        old_key = os.environ["NORI_BACKUP_KEY"]
        os.environ["NORI_BACKUP_KEY"] = passphrase
        try:
            rec = backup.create_backup(app="nori")
        finally:
            os.environ["NORI_BACKUP_KEY"] = old_key
        self.assertEqual(rec["status"], "ok")
        bad_target = Path(tempfile.mkdtemp(prefix="nori_test_backup_badpass_"))
        self.addCleanup(shutil.rmtree, bad_target, ignore_errors=True)
        env = dict(os.environ)
        env["NORI_BACKUP_KEY"] = "a totally different five word phrase"  # still 20+ chars, still wrong
        result = subprocess.run(
            [sys.executable, str(ROOT / "restore_backup.py"), rec["path"],
             "--target", str(bad_target), "--yes"],
            capture_output=True, text=True, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("couldn't decrypt", result.stderr)
        self.assertEqual(list(bad_target.iterdir()), [])

    def test_raw_key_mode_still_works_detected_by_shape(self):
        """The original design (a raw generated Fernet key) stays fully
        supported, auto-detected by its own exact shape -- this is the
        module-level NORI_BACKUP_KEY every other test in this class already
        uses, so this just asserts the header records raw-key mode."""
        rec = backup.create_backup(app="nori")
        self.assertEqual(rec["status"], "ok")
        raw = Path(rec["path"]).read_bytes()
        self.assertTrue(raw.startswith(backup._ARCHIVE_MAGIC))
        self.assertEqual(raw[len(backup._ARCHIVE_MAGIC)], backup._MODE_RAW_KEY)

    def test_prune_local_keeps_recent_removes_old(self):
        recent = backup.create_backup(app="nori")
        old_path = backup.BACKUP_DIR / "nori-backup-20200101-000000.tar.gz.enc"
        old_path.write_bytes(b"stale")
        os.utime(old_path, (time.time() - 40 * 86400,) * 2)
        removed = backup.prune_local(retention_days=14)
        self.assertEqual(removed, 1)
        self.assertFalse(old_path.exists())
        self.assertTrue(Path(recent["path"]).exists())


class RemoteMockedTests(unittest.TestCase):
    """No live Google/Microsoft account has ever been connected on this
    instance -- these verify request SHAPE via a monkeypatched
    authed_request, same honesty as the rest of this connector layer."""

    def test_upload_one_google_drive(self):
        archive = backup.BACKUP_DIR / "nori-backup-20260101-000000.tar.gz.enc"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"fake archive bytes")

        def fake(user_id, provider, url, **kw):
            if "files?q=" in url:
                return _ok({"files": []})
            if kw.get("method") == "POST" and "mimeType" in str(kw.get("body")):
                return _ok({"id": "folder1"})
            if "upload/drive/v3/files" in url:
                return _ok({"id": "uploaded1"})
            return _ok({})

        with patch.object(connected_accounts, "authed_request", side_effect=fake):
            result = backup.upload_one(archive, provider="google_drive")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result["file_id"], "uploaded1")

    def test_upload_one_unknown_provider(self):
        archive = backup.BACKUP_DIR / "x.tar.gz.enc"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"x")
        result = backup.upload_one(archive, provider="dropbox")
        self.assertIn("error", result)

    def test_remote_prune_candidates_filters_by_age_and_name(self):
        old_iso = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=40)).isoformat()
        new_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        def fake(user_id, provider, url, **kw):
            if "fields=files(id,name)" in url:
                return _ok({"files": [{"id": "folderX", "name": "NoriBackups"}]})
            return _ok({"files": [
                {"id": "old1", "name": "nori-backup-20200101-000000.tar.gz.enc", "modifiedTime": old_iso},
                {"id": "new1", "name": "nori-backup-20260916-000000.tar.gz.enc", "modifiedTime": new_iso},
                {"id": "other1", "name": "unrelated.txt", "modifiedTime": old_iso},
            ]})

        with patch.object(connected_accounts, "authed_request", side_effect=fake):
            result = backup.remote_prune_candidates("google_drive", retention_days=14)
        self.assertTrue(result["ok"])
        self.assertEqual({c["name"] for c in result["candidates"]},
                         {"nori-backup-20200101-000000.tar.gz.enc"})

    def test_remote_prune_execute_deletes_and_logs(self):
        def fake(user_id, provider, url, **kw):
            if kw.get("method") == "DELETE":
                return {"ok": True, "data": b""}
            return _ok({})

        with patch.object(connected_accounts, "authed_request", side_effect=fake):
            result = backup.remote_prune_execute("google_drive", ["old1"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"], ["old1"])
        self.assertTrue(any(r.get("action") == "prune" for r in backup.history()))


if __name__ == "__main__":
    unittest.main()

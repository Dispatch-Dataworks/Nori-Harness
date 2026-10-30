# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The server fetches the Android app from the newest release (app_fetch.py, 2026-10-29). Real SQLite, config and files; only the HTTP download is faked.

Pinned: only a release asset that is exactly this repo's own nori-companion-<version>.apk download is ever stored or fetched; the newest version wins
and never a downgrade or a repeat; a file that is the wrong size, fails GitHub's checksum or is not an Android app is thrown away and leaves the app on
offer untouched; a failure cools down before it is retried; redirects may only go to GitHub; the switches (the update check, the app fetch, the
pre-release choice) are honoured; the scheduler runs it; and the Updates tab shows it and its button works."""
import hashlib
import io
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_app_fetch_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_app_fetch_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"
os.environ.pop("NORI_UPDATE_REPO", None)

import accounts  # noqa: E402
import app_fetch  # noqa: E402
import config  # noqa: E402
import devices  # noqa: E402
import scheduler  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402
import updates  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Tester", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
SESS = {"user_id": UID, "workspace_id": WS, "role": "admin", "csrf": "csrf-tok"}
BASE = "https://github.com/Dispatch-Dataworks/Nori-Harness/"


def make_apk(extra: bytes = b"") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("AndroidManifest.xml", b"manifest" + extra)
        z.writestr("classes.dex", b"dex" + extra)
    return buf.getvalue()


def asset(version: str, data: bytes, tag: str, *, digest: bool = True, name: str | None = None, url: str | None = None) -> dict:
    name = name or f"nori-companion-{version}.apk"
    return {"name": name, "size": len(data), "browser_download_url": url or f"{BASE}releases/download/{tag}/{name}",
            **({"digest": "sha256:" + hashlib.sha256(data).hexdigest()} if digest else {})}


def release(tag, assets=(), *, pre=True):
    return {"tag_name": tag, "name": tag, "prerelease": pre, "draft": False, "body": "", "html_url": f"{BASE}releases/tag/{tag}",
            "published_at": "2026-10-29T12:00:00Z", "assets": list(assets)}


def seed(*releases):
    updates._save(checked_ts=time.time(), next_check_ts=time.time() + 3600, etag="e", releases=updates._clean_releases(list(releases)), error="")


def offered_version():
    return devices.apk_version() if devices.apk_path() else None


def reset():
    store.write(lambda c: c.execute("DELETE FROM update_check"))
    for k, v in (("update_check_enabled", True), ("update_include_prereleases", True), ("phone_app_auto_fetch", True)):
        config.set("workspace", WS, k, v)
    d = devices.apk_dir()
    if d.is_dir():
        for p in d.iterdir():
            p.unlink()


class Fake:
    """Replaces app_fetch._download: serves `blobs` by URL and records what was asked for."""
    def __init__(self, blobs):
        self.blobs, self.asked = blobs, []

    def __call__(self, url, limit):
        self.asked.append((url, limit))
        if url not in self.blobs:
            raise OSError("404")
        v = self.blobs[url]
        if isinstance(v, Exception):
            raise v
        return v


class WhatIsStored(unittest.TestCase):
    def setUp(self):
        reset()

    def test_only_this_repos_own_apk_asset_is_kept(self):
        good = make_apk()
        rels = updates._clean_releases([release("v1.0.0-beta", [
            asset("0.2.0", good, "v1.0.0-beta"),
            asset("0.2.0", good, "v1.0.0-beta", name="other-app-0.2.0.apk"),
            asset("0.2.0", good, "v1.0.0-beta", url="https://evil.example.com/releases/download/v1.0.0-beta/nori-companion-0.2.0.apk"),
            asset("0.2.0", good, "v1.0.0-beta", url=BASE + "releases/download/v9.9.9/nori-companion-0.2.0.apk"),
            asset("0.2.0", good, "v1.0.0-beta", name="nori-companion-0.2.0.apk.exe", url=BASE + "releases/download/v1.0.0-beta/nori-companion-0.2.0.apk.exe"),
            {"name": "nori-companion-0.3.0.apk", "size": 10**9, "browser_download_url": BASE + "releases/download/v1.0.0-beta/nori-companion-0.3.0.apk"},
            {"name": "nori-companion-0.4.0.apk", "size": 0, "browser_download_url": BASE + "releases/download/v1.0.0-beta/nori-companion-0.4.0.apk"},
            "junk"])])
        self.assertEqual([a["name"] for a in rels[0]["assets"]], ["nori-companion-0.2.0.apk"])
        self.assertTrue(rels[0]["assets"][0]["digest"].startswith("sha256:"))

    def test_a_malformed_digest_is_dropped_not_trusted(self):
        data = make_apk()
        a = asset("0.2.0", data, "v1.0.0-beta")
        a["digest"] = "md5:abc"
        self.assertEqual(updates._clean_releases([release("v1.0.0-beta", [a])])[0]["assets"][0]["digest"], "")

    def test_a_release_list_stored_before_assets_existed_is_asked_for_again_not_trusted_as_unchanged(self):
        updates._save(checked_ts=time.time(), next_check_ts=time.time() + 86400, etag="old",
                      releases=[{"tag": "v0.13.0-beta", "name": "x", "prerelease": True, "url": "", "published": "", "notes": ""}], error="")
        seen = {}

        def fake_fetch(etag):
            seen["etag"] = etag
            return 200, "new", [release("v0.13.0-beta", [asset("0.2.0", make_apk(), "v0.13.0-beta")])]
        with patch.object(updates, "_fetch", fake_fetch):
            self.assertTrue(updates.maybe_check(), "due now even though the daily timer had hours left")
        self.assertEqual(seen["etag"], "", "an unconditional request, so a 304 can't leave the list without assets")
        self.assertEqual(len(updates._row()["releases"][0]["assets"]), 1)
        with patch.object(updates, "_fetch", fake_fetch):
            self.assertFalse(updates.maybe_check(), "and once it has them it waits for the daily timer again")


class Fetching(unittest.TestCase):
    def setUp(self):
        reset()
        self.apk = make_apk()
        self.url = BASE + "releases/download/v0.14.0-beta/nori-companion-0.2.0.apk"
        seed(release("v0.14.0-beta", [asset("0.2.0", self.apk, "v0.14.0-beta")]))
        self.fake = Fake({self.url: self.apk})
        p = patch.object(app_fetch, "_download", self.fake)
        p.start()
        self.addCleanup(p.stop)

    def test_a_server_with_no_app_gets_the_newest_one_and_phones_are_offered_it(self):
        self.assertIsNone(devices.app_info())
        self.assertTrue(app_fetch.maybe_fetch())
        self.assertEqual(devices.apk_path().read_bytes(), self.apk)
        self.assertEqual(devices.app_info(), {"latest": "0.2.0", "apk_url": "/device/app.apk"})
        self.assertEqual(self.fake.asked, [(self.url, len(self.apk))])
        self.assertFalse(list(devices.apk_dir().glob("*.tmp")), "no half-written leftovers")

    def test_it_does_not_fetch_again_once_it_has_that_version(self):
        self.assertTrue(app_fetch.maybe_fetch())
        self.assertFalse(app_fetch.maybe_fetch())
        self.assertEqual(len(self.fake.asked), 1)

    def test_a_newer_app_replaces_the_old_one_but_an_older_one_never_does(self):
        app_fetch.maybe_fetch()
        newer = make_apk(b"v3")
        url3 = BASE + "releases/download/v0.15.0-beta/nori-companion-0.3.0.apk"
        self.fake.blobs[url3] = newer
        seed(release("v0.14.0-beta", [asset("0.2.0", self.apk, "v0.14.0-beta")]), release("v0.15.0-beta", [asset("0.3.0", newer, "v0.15.0-beta")]))
        self.assertTrue(app_fetch.maybe_fetch())
        self.assertEqual((offered_version(), devices.apk_path().read_bytes()), ("0.3.0", newer))
        seed(release("v0.14.0-beta", [asset("0.2.0", self.apk, "v0.14.0-beta")]))      # the newest release now carries an older app
        self.assertFalse(app_fetch.maybe_fetch())
        self.assertEqual(offered_version(), "0.3.0")

    def test_the_newest_app_wins_whichever_release_carries_it(self):
        old = make_apk(b"old")
        seed(release("v0.15.0-beta"),                                                    # the newest release attached nothing
             release("v0.14.0-beta", [asset("0.2.0", self.apk, "v0.14.0-beta")]),
             release("v0.13.0-beta", [asset("0.1.0", old, "v0.13.0-beta")]))
        self.assertEqual(app_fetch.wanted()["version"], "0.2.0")

    def test_a_hand_uploaded_newer_app_is_left_alone(self):
        devices.apk_dir().mkdir(parents=True, exist_ok=True)
        (devices.apk_dir() / "nori-companion.apk").write_bytes(b"hand built")
        (devices.apk_dir() / "version.txt").write_text("0.9.0")
        self.assertFalse(app_fetch.maybe_fetch())
        self.assertEqual((devices.apk_dir() / "nori-companion.apk").read_bytes(), b"hand built")

    def test_a_version_file_with_no_apk_beside_it_does_not_count_as_having_one(self):
        devices.apk_dir().mkdir(parents=True, exist_ok=True)
        (devices.apk_dir() / "version.txt").write_text("0.2.0")
        self.assertTrue(app_fetch.maybe_fetch())
        self.assertIsNotNone(devices.apk_path())

    def test_a_prerelease_app_waits_for_the_prerelease_switch(self):
        config.set("workspace", WS, "update_include_prereleases", False)
        self.assertFalse(app_fetch.maybe_fetch())
        self.assertIsNone(devices.app_info())
        config.set("workspace", WS, "update_include_prereleases", True)
        self.assertTrue(app_fetch.maybe_fetch())

    def test_either_switch_off_means_no_download(self):
        config.set("workspace", WS, "phone_app_auto_fetch", False)
        self.assertFalse(app_fetch.maybe_fetch())
        config.set("workspace", WS, "phone_app_auto_fetch", True)
        config.set("workspace", WS, "update_check_enabled", False)
        self.assertFalse(app_fetch.maybe_fetch())
        self.assertEqual(self.fake.asked, [])

    def test_the_scheduler_runs_it(self):
        scheduler._maybe_check_updates()
        self.assertEqual(offered_version(), "0.2.0")


class WhenItGoesWrong(unittest.TestCase):
    def setUp(self):
        reset()
        self.apk = make_apk()
        self.url = BASE + "releases/download/v0.14.0-beta/nori-companion-0.2.0.apk"

    def _try(self, served, **asset_kw):
        seed(release("v0.14.0-beta", [asset("0.2.0", self.apk, "v0.14.0-beta", **asset_kw)]))
        fake = Fake({self.url: served})
        with patch.object(app_fetch, "_download", fake):
            return app_fetch.maybe_fetch(), fake

    def _old_app_on_offer(self):
        devices.apk_dir().mkdir(parents=True, exist_ok=True)
        (devices.apk_dir() / "nori-companion.apk").write_bytes(b"old app")
        (devices.apk_dir() / "version.txt").write_text("0.1.0")

    def test_a_download_that_fails_leaves_the_old_app_and_says_why(self):
        self._old_app_on_offer()
        attempted, _ = self._try(OSError("connection reset"))
        self.assertTrue(attempted)
        self.assertEqual((devices.apk_path().read_bytes(), offered_version()), (b"old app", "0.1.0"))
        last = app_fetch.status(WS)["last"]
        self.assertFalse(last["ok"])
        self.assertIn("connection reset", last["error"])

    def test_a_failure_is_not_retried_every_cycle_but_is_after_the_cooldown(self):
        self._try(OSError("down"))
        attempted, fake = self._try(OSError("down"))
        self.assertFalse(attempted)
        self.assertEqual(fake.asked, [])
        st = app_fetch._state()
        st["next_try_ts"] = time.time() - 1
        app_fetch._record(**st)
        attempted, _ = self._try(self.apk)
        self.assertTrue(attempted)
        self.assertEqual(offered_version(), "0.2.0")

    def test_a_new_version_is_tried_at_once_even_if_the_last_one_failed_recently(self):
        self._try(OSError("down"))
        newer = make_apk(b"3")
        seed(release("v0.15.0-beta", [asset("0.3.0", newer, "v0.15.0-beta")]))
        with patch.object(app_fetch, "_download", Fake({BASE + "releases/download/v0.15.0-beta/nori-companion-0.3.0.apk": newer})):
            self.assertTrue(app_fetch.maybe_fetch())
        self.assertEqual(offered_version(), "0.3.0")

    def test_a_file_that_fails_githubs_checksum_is_thrown_away(self):
        self._old_app_on_offer()
        tampered = self.apk.replace(b"manifest", b"MANIFEST")      # same size, different bytes
        self.assertEqual(len(tampered), len(self.apk))
        attempted, _ = self._try(tampered, digest=True)
        self.assertTrue(attempted)
        self.assertEqual(offered_version(), "0.1.0")
        self.assertIn("checksum", app_fetch.status(WS)["last"]["error"])

    def test_a_file_of_the_wrong_size_is_thrown_away(self):
        attempted, _ = self._try(self.apk + b"extra", digest=False)
        self.assertIsNone(devices.app_info())
        self.assertIn("bytes", app_fetch.status(WS)["last"]["error"])

    def test_something_that_is_not_an_android_app_is_thrown_away_even_with_a_matching_checksum(self):
        for junk in (b"MZ not a zip at all", self._zip_without_manifest()):
            reset()
            data = junk
            seed(release("v0.14.0-beta", [asset("0.2.0", data, "v0.14.0-beta")]))
            with patch.object(app_fetch, "_download", Fake({self.url: data})):
                self.assertTrue(app_fetch.maybe_fetch())
            self.assertIsNone(devices.app_info(), junk[:10])
            self.assertIn("not an Android app", app_fetch.status(WS)["last"]["error"])

    @staticmethod
    def _zip_without_manifest():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("readme.txt", b"hi")
        return buf.getvalue()

    def test_the_download_is_capped_at_the_size_github_listed(self):
        seed(release("v0.14.0-beta", [asset("0.2.0", self.apk, "v0.14.0-beta")]))
        fake = Fake({self.url: self.apk})
        with patch.object(app_fetch, "_download", fake):
            app_fetch.maybe_fetch()
        self.assertEqual(fake.asked[0][1], len(self.apk))


class Network(unittest.TestCase):
    def test_hosts(self):
        for ok in ("https://github.com/a/b/releases/download/v1/x.apk", "https://objects.githubusercontent.com/x",
                   "https://release-assets.githubusercontent.com/github-production-release-asset/1"):
            self.assertTrue(app_fetch.host_ok(ok), ok)
        for bad in ("http://github.com/x", "https://evil.example.com/x", "https://github.com.evil.example.com/x", "https://githubusercontent.com.evil.io/x",
                    "https://user@github.com/x", "ftp://github.com/x", "file:///etc/passwd", "", "https://notgithub.com/x", "https://evilgithubusercontent.com/x"):
            self.assertFalse(app_fetch.host_ok(bad), bad)

    def test_a_redirect_off_github_is_refused(self):
        h = app_fetch._GithubOnlyRedirects()
        req = urllib.request.Request("https://github.com/a/b/releases/download/v1/x.apk")
        with self.assertRaises(urllib.error.URLError):
            h.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/x.apk")
        nxt = h.redirect_request(req, None, 302, "Found", {}, "https://objects.githubusercontent.com/x.apk")
        self.assertEqual(nxt.full_url, "https://objects.githubusercontent.com/x.apk")

    def test_the_downloader_itself_refuses_a_non_github_address(self):
        with self.assertRaises(ValueError):
            app_fetch._download("https://evil.example.com/x.apk", 100)


class UpdatesTab(unittest.TestCase):
    def setUp(self):
        reset()
        self.apk = make_apk()
        seed(release("v0.14.0-beta", [asset("0.2.0", self.apk, "v0.14.0-beta")]))
        self.url = BASE + "releases/download/v0.14.0-beta/nori-companion-0.2.0.apk"

    def _page(self):
        sent = {}
        h = server.Handler.__new__(server.Handler)
        h.send = lambda status, body, *a, **k: sent.update(status=status, body=body)
        h.forbidden = lambda: sent.update(status=403)
        h.headers = {}
        h.updates_admin_form(SESS)
        return sent

    def test_the_tab_shows_what_is_offered_and_what_is_newest(self):
        page = self._page()["body"].decode()
        self.assertIn("this server has no app to offer yet", page)
        self.assertIn("<b>0.2.0</b>", page)
        self.assertIn("name=phone_app_auto_fetch checked", page)
        self.assertIn("/admin/updates/appfetch", page)

    def test_the_button_fetches_and_the_tab_then_reports_it(self):
        sent = {}
        h = server.Handler.__new__(server.Handler)
        h.send = lambda status, body, *a, **k: sent.update(status=status, body=body)
        h.forbidden = lambda: None
        h.headers = {}
        with patch.object(app_fetch, "_download", Fake({self.url: self.apk})):
            h.updates_post(SESS, "/admin/updates/appfetch", {})
        self.assertEqual(offered_version(), "0.2.0")
        page = sent["body"].decode()
        self.assertIn("phones are now offered app 0.2.0", page)
        self.assertIn("this server offers <b>0.2.0</b>", page)

    def test_the_button_works_even_with_the_automatic_switch_off_and_says_when_there_is_nothing_to_do(self):
        config.set("workspace", WS, "phone_app_auto_fetch", False)
        with patch.object(app_fetch, "_download", Fake({self.url: self.apk})):
            self.assertTrue(app_fetch.fetch_now(WS)["ok"])
            self.assertEqual(offered_version(), "0.2.0")
            again = app_fetch.fetch_now(WS)
        self.assertEqual(again, {"ok": True, "message": "this server already offers app 0.2.0"})

    def test_the_button_reports_a_failure_and_a_release_with_no_app(self):
        with patch.object(app_fetch, "_download", Fake({})):
            res = app_fetch.fetch_now(WS)
        self.assertFalse(res["ok"])
        seed(release("v0.14.0-beta"))
        self.assertEqual(app_fetch.fetch_now(WS), {"ok": False, "message": "no release has an Android app attached"})

    def test_saving_the_switch(self):
        sent = {}
        h = server.Handler.__new__(server.Handler)
        h.send = lambda status, body, *a, **k: sent.update(status=status, body=body)
        h.forbidden = lambda: None
        h.headers = {}
        h.updates_post(SESS, "/admin/updates/settings", {"update_check_enabled": "on"})
        self.assertFalse(config.get("workspace", WS, "phone_app_auto_fetch"))
        h.updates_post(SESS, "/admin/updates/settings", {"update_check_enabled": "on", "phone_app_auto_fetch": "on"})
        self.assertTrue(config.get("workspace", WS, "phone_app_auto_fetch"))

    def test_a_member_cannot_use_the_button(self):
        member = dict(SESS, role="member")
        sent = {}
        h = server.Handler.__new__(server.Handler)
        h.forbidden = lambda: sent.update(status=403)
        h.send = lambda *a, **k: sent.update(status="sent")
        h.updates_post(member, "/admin/updates/appfetch", {})
        self.assertEqual(sent["status"], 403)
        self.assertIsNone(devices.app_info())


import urllib.request  # noqa: E402  (used by Network)

if __name__ == "__main__":
    unittest.main()

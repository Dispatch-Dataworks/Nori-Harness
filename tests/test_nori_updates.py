# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""\"A newer Nori is available\" (2026-10-26, updates.py). Real SQLite and config; only the HTTP request to GitHub is faked.

What is pinned: versions compare the way semver does (a release outranks its pre-releases, beta.10 > beta.2); only published releases with a version
tag count (drafts, odd tags and foreign links are dropped); a 304 keeps what was known; an error is recorded and retried sooner without losing the
last good answer; the check is off when switched off; the admin sees a chip and the exact commands, a member sees nothing; and release notes
(untrusted text from GitHub) are rendered escaped."""
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_updates_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_updates_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"
os.environ.pop("NORI_VERSION", None)
os.environ.pop("NORI_UPDATE_REPO", None)

import accounts  # noqa: E402
import config  # noqa: E402
import scheduler  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402
import updates  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Tester", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
_inv_id, _inv_tok = accounts.create_invite(WS, "Second", "member", UID)
_member = accounts.accept_invite(_inv_tok, "memberpass123")
SESS = {"user_id": UID, "workspace_id": WS, "role": "admin", "csrf": "csrf-tok"}
MEM = {"user_id": _member["id"], "workspace_id": WS, "role": "member", "csrf": "csrf-mem"}
BASE = "https://github.com/Dispatch-Dataworks/Nori-Harness/"


def _rel(tag, *, pre=False, draft=False, notes="notes", url=None):
    return {"tag_name": tag, "name": f"Nori {tag}", "prerelease": pre, "draft": draft, "body": notes,
            "html_url": url if url is not None else f"{BASE}releases/tag/{tag}", "published_at": "2026-10-26T12:00:00Z"}


def _reset():
    store.write(lambda c: c.execute("DELETE FROM update_check"))
    for k, v in (("update_check_enabled", True), ("update_include_prereleases", True), ("update_dismissed_version", "")):
        config.set("workspace", WS, k, v)


def _seed(*releases, etag="e1"):
    updates._save(checked_ts=time.time(), next_check_ts=time.time() + 3600, etag=etag, releases=updates._clean_releases(list(releases)), error="")


class Versions(unittest.TestCase):
    def test_ordering_follows_semver(self):
        order = ["1.0.0-alpha", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.10.0", "2.0.0-beta", "2.0.0"]
        keys = [updates.parse_version(v) for v in order]
        self.assertEqual(keys, sorted(keys))
        self.assertTrue(updates.parse_version("0.10.0-beta") > updates.parse_version("0.9.0-beta"), "0.10 is later than 0.9, not earlier")
        self.assertTrue(updates.parse_version("v0.9.0") > updates.parse_version("0.9.0-beta"))

    def test_the_v_prefix_and_build_metadata_are_accepted(self):
        self.assertEqual(updates.parse_version("v1.2.3"), updates.parse_version("1.2.3"))
        self.assertEqual(updates.parse_version("1.2.3+build5"), updates.parse_version("1.2.3"))

    def test_junk_is_not_a_version(self):
        for s in ("", "latest", "1.2", "v1", "nightly-2026", "1.2.3.4", None, 5, "1.2.3 rm -rf"):
            self.assertIsNone(updates.parse_version(s), repr(s))

    def test_the_running_version_comes_from_the_version_file_and_can_be_overridden(self):
        self.assertEqual(updates.current_version(), (ROOT / "VERSION").read_text().strip())
        self.assertIsNotNone(updates.parse_version(updates.current_version()))
        with patch.dict(os.environ, {"NORI_VERSION": "9.9.9"}):
            self.assertEqual(updates.current_version(), "9.9.9")
        with patch.dict(os.environ, {"NORI_VERSION": "not a version"}), patch.object(updates, "HERE", Path(tempfile.mkdtemp())):
            self.assertEqual(updates.current_version(), "0.0.0-unknown")


class Cleaning(unittest.TestCase):
    def test_only_published_releases_with_version_tags_survive(self):
        got = updates._clean_releases([_rel("v1.0.0"), _rel("v1.1.0", draft=True), _rel("nightly"), _rel("v1.2.0-beta", pre=True), "junk", None])
        self.assertEqual([r["tag"] for r in got], ["v1.0.0", "v1.2.0-beta"])
        self.assertTrue(got[1]["prerelease"])

    def test_a_link_off_the_project_is_dropped_and_notes_are_capped(self):
        got = updates._clean_releases([_rel("v1.0.0", url="https://evil.example/x"), _rel("v1.0.1", notes="x" * 50000)])
        self.assertEqual(got[0]["url"], "")
        self.assertEqual(len(got[1]["notes"]), updates.NOTES_CAP)
        self.assertTrue(got[1]["url"].startswith(BASE))

    def test_a_non_list_answer_is_empty(self):
        self.assertEqual(updates._clean_releases({"message": "Not Found"}), [])


class _Resp:
    def __init__(self, body, etag="etag-1", status=200):
        self._b, self.status, self.headers = body, status, {"ETag": etag}

    def read(self, n=-1):
        return self._b[:n] if n and n > 0 else self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Checking(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_a_check_stores_the_answer_and_asks_politely(self):
        seen = {}

        def fake(req, timeout):
            seen["url"], seen["headers"], seen["timeout"] = req.full_url, dict(req.header_items()), timeout
            return _Resp(json.dumps([_rel("v0.11.0-beta", pre=True)]).encode(), etag="W/abc")

        with patch("urllib.request.urlopen", fake):
            self.assertEqual(updates.check(), {"ok": True, "changed": True})
        self.assertEqual(seen["url"], "https://api.github.com/repos/Dispatch-Dataworks/Nori-Harness/releases?per_page=10")
        self.assertEqual(seen["headers"]["User-agent"], "Nori-update-check")
        self.assertNotIn("If-none-match", seen["headers"], "nothing to be conditional on the first time")
        self.assertLessEqual(seen["timeout"], 10)
        row = updates._row()
        self.assertEqual((row["etag"], [r["tag"] for r in row["releases"]], row["error"]), ("W/abc", ["v0.11.0-beta"], ""))
        self.assertGreater(row["next_check_ts"], time.time() + 23 * 3600)

    def test_nothing_identifying_is_sent(self):
        seen = {}

        def fake(req, timeout):
            seen["req"] = req
            return _Resp(b"[]")

        with patch("urllib.request.urlopen", fake):
            updates.check()
        r = seen["req"]
        self.assertIsNone(r.data, "a plain GET with no body")
        self.assertEqual(set(k.lower() for k in dict(r.header_items())), {"accept", "user-agent", "x-github-api-version"})

    def test_a_304_keeps_what_was_known_and_sends_the_etag(self):
        _seed(_rel("v1.0.0"), etag="W/known")
        seen = {}

        def fake(req, timeout):
            seen["inm"] = dict(req.header_items()).get("If-none-match")
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, io.BytesIO(b""))

        with patch("urllib.request.urlopen", fake):
            self.assertEqual(updates.check(), {"ok": True, "changed": False})
        self.assertEqual(seen["inm"], "W/known")
        self.assertEqual([r["tag"] for r in updates._row()["releases"]], ["v1.0.0"])

    def test_a_failure_is_recorded_retried_sooner_and_keeps_the_last_good_answer(self):
        _seed(_rel("v1.0.0"))
        for exc in (urllib.error.URLError("no route"), TimeoutError("slow"),
                    urllib.error.HTTPError("u", 403, "rate", {}, io.BytesIO(b""))):
            with patch("urllib.request.urlopen", side_effect=exc):
                res = updates.check()
            self.assertFalse(res["ok"])
            row = updates._row()
            self.assertTrue(row["error"])
            self.assertEqual([r["tag"] for r in row["releases"]], ["v1.0.0"])
            self.assertLess(row["next_check_ts"], time.time() + 7 * 3600)
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("u", 403, "rate", {}, io.BytesIO(b""))):
            self.assertIn("rate limited", updates.check()["error"])

    def test_garbage_and_oversized_answers_are_errors_not_crashes(self):
        with patch("urllib.request.urlopen", lambda req, timeout: _Resp(b"<html>not json")):
            self.assertFalse(updates.check()["ok"])
        with patch("urllib.request.urlopen", lambda req, timeout: _Resp(b"[" + b" " * (updates.MAX_BODY_BYTES + 10) + b"]")):
            self.assertFalse(updates.check()["ok"])

    def test_maybe_check_respects_the_switch_and_the_schedule(self):
        calls = []

        def fake(req, timeout):
            calls.append(1)
            return _Resp(b"[]")

        with patch("urllib.request.urlopen", fake):
            config.set("workspace", WS, "update_check_enabled", False)
            self.assertFalse(updates.maybe_check())
            self.assertEqual(calls, [], "off means no request at all")
            config.set("workspace", WS, "update_check_enabled", True)
            self.assertTrue(updates.maybe_check())
            self.assertFalse(updates.maybe_check(), "not due again for a day")
            self.assertEqual(len(calls), 1)
            updates._save(next_check_ts=time.time() - 1)
            self.assertTrue(updates.maybe_check())

    def test_the_scheduler_hook_never_raises(self):
        with patch.object(updates, "maybe_check", side_effect=RuntimeError("boom")):
            scheduler._maybe_check_updates()
        with patch.object(updates, "maybe_check") as m:
            scheduler._maybe_check_updates()
        m.assert_called_once()

    def test_a_forks_repo_can_be_named_but_only_a_valid_one(self):
        with patch.dict(os.environ, {"NORI_UPDATE_REPO": "someone/Nori-fork"}):
            self.assertIn("/repos/someone/Nori-fork/releases", updates.api_url())
        with patch.dict(os.environ, {"NORI_UPDATE_REPO": "../../evil?x=1"}):
            self.assertEqual(updates.repo(), "Dispatch-Dataworks/Nori-Harness")


class Status(unittest.TestCase):
    def setUp(self):
        _reset()
        self.cur = updates.current_version()

    def test_a_newer_release_is_available_and_the_chip_shows(self):
        _seed(_rel("v99.0.0"))
        st = updates.status(WS)
        self.assertTrue(st["available"])
        self.assertEqual((st["latest"], st["current"]), ("v99.0.0", self.cur))
        self.assertEqual(updates.banner_for(WS)["tag"], "v99.0.0")

    def test_the_same_or_an_older_release_is_not(self):
        _seed(_rel("v" + self.cur), _rel("v0.0.1"))
        self.assertFalse(updates.status(WS)["available"])
        self.assertIsNone(updates.banner_for(WS))

    def test_the_newest_of_several_is_the_one_offered(self):
        _seed(_rel("v99.1.0"), _rel("v99.3.0"), _rel("v99.2.0"))
        self.assertEqual(updates.status(WS)["latest"], "v99.3.0")

    def test_prereleases_can_be_left_out(self):
        _seed(_rel("v99.0.0-beta", pre=True))
        self.assertTrue(updates.status(WS)["available"])
        config.set("workspace", WS, "update_include_prereleases", False)
        self.assertFalse(updates.status(WS)["available"])
        _seed(_rel("v99.0.0-beta", pre=True), _rel("v98.0.0"))
        self.assertEqual(updates.status(WS)["latest"], "v98.0.0")

    def test_skipping_a_release_hides_the_chip_until_a_newer_one(self):
        _seed(_rel("v99.0.0"))
        self.assertTrue(updates.dismiss(WS))
        self.assertIsNone(updates.banner_for(WS))
        self.assertTrue(updates.status(WS)["dismissed"])
        self.assertTrue(updates.status(WS)["available"], "still available, just not announced")
        _seed(_rel("v99.0.0"), _rel("v99.1.0"))
        self.assertEqual(updates.banner_for(WS)["tag"], "v99.1.0")

    def test_nothing_to_skip_when_up_to_date(self):
        self.assertFalse(updates.dismiss(WS))

    def test_switched_off_means_no_chip_even_if_a_release_is_known(self):
        _seed(_rel("v99.0.0"))
        config.set("workspace", WS, "update_check_enabled", False)
        self.assertIsNone(updates.banner_for(WS))

    def test_the_commands_name_the_tag_and_a_bad_tag_never_reaches_a_command(self):
        steps = updates.update_steps("v1.2.3")
        self.assertEqual(steps["docker"], ["git fetch --tags", "git checkout v1.2.3", "docker compose up -d --build"])
        self.assertIn("git checkout v1.2.3", steps["plain"])
        for bad in ("v1.2.3; rm -rf /", "$(x)", "", None):
            self.assertIn("git pull", updates.update_steps(bad)["docker"])
            self.assertFalse(any("rm" in c or "$(" in c for c in updates.update_steps(bad)["docker"]))


class _H(server.Handler):
    def __init__(self):
        self.headers = {}
        self.sent, self.page = [], None

    def send(self, code, body=b"", extra=None, *, ctype="text/html; charset=utf-8"):
        self.sent.append({"code": code, "body": body})

    def _settings_response(self, sess, tab, main):
        self.page, self.tab = main, tab

    def _board_count(self, sess):
        return 0

    def _hdr_menu(self, sess):
        return ""


class Pages(unittest.TestCase):
    def setUp(self):
        _reset()

    def _form(self, sess=SESS):
        h = _H()
        h.updates_admin_form(sess)
        return h

    def test_a_fresh_install_says_it_has_not_checked_yet(self):
        h = self._form()
        self.assertEqual(h.tab, "updates")
        self.assertIn("Not checked yet", h.page)
        self.assertIn(updates.current_version(), h.page)

    def test_up_to_date_page(self):
        _seed(_rel("v0.0.1"))
        h = self._form()
        self.assertIn("up to date", h.page)
        self.assertIn("last checked", h.page)
        self.assertNotIn("how to update", h.page)

    def test_available_page_shows_notes_link_and_the_exact_commands(self):
        _seed(_rel("v99.0.0", notes="Fixes things.\n- a\n- b"))
        page = self._form().page
        for needle in ("update available", "v99.0.0", "Fixes things.", "view on GitHub", "git fetch --tags", "git checkout v99.0.0",
                       "docker compose up -d --build", "pip install -r requirements.txt", "skip this release", "check now"):
            self.assertIn(needle, page, needle)
        self.assertIn(f"href='{BASE}releases/tag/v99.0.0'", page)
        self.assertIn("rel='noopener noreferrer'", page)

    def test_release_notes_are_untrusted_text_and_rendered_escaped(self):
        _seed(_rel("v99.0.0", notes="<script>alert(1)</script><img src=x onerror=alert(2)>"))
        page = self._form().page
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_a_prerelease_is_labelled_and_a_skipped_release_is_explained(self):
        _seed(_rel("v99.0.0-beta", pre=True))
        self.assertIn("(pre-release)", self._form().page)
        updates.dismiss(WS)
        self.assertIn("You skipped this release", self._form().page)

    def test_the_last_error_is_shown(self):
        updates._save(checked_ts=time.time(), error="GitHub answered 500")
        self.assertIn("The last check failed: GitHub answered 500", self._form().page)

    def test_members_get_nothing(self):
        h = _H()
        h.forbidden = lambda: h.sent.append({"code": 403, "body": b""})
        h.updates_admin_form(MEM)
        self.assertEqual(h.sent[-1]["code"], 403)
        self.assertIsNone(h.page)
        for action in ("settings", "check", "dismiss"):
            h = _H()
            h.forbidden = lambda h=h: h.sent.append({"code": 403, "body": b""})
            h.updates_post(MEM, "/admin/updates/" + action, {"update_check_enabled": "on"})
            self.assertEqual(h.sent[-1]["code"], 403, action)
        self.assertTrue(config.get("workspace", WS, "update_check_enabled"))

    def test_settings_checkboxes(self):
        h = _H()
        h.updates_post(SESS, "/admin/updates/settings", {})
        self.assertEqual((config.get("workspace", WS, "update_check_enabled"), config.get("workspace", WS, "update_include_prereleases")),
                         (False, False))
        h.updates_post(SESS, "/admin/updates/settings", {"update_check_enabled": "on", "update_include_prereleases": "on"})
        self.assertEqual((config.get("workspace", WS, "update_check_enabled"), config.get("workspace", WS, "update_include_prereleases")),
                         (True, True))

    def test_check_now_and_skip_buttons(self):
        h = _H()
        with patch("urllib.request.urlopen", lambda req, timeout: _Resp(json.dumps([_rel("v99.0.0")]).encode())):
            h.updates_post(SESS, "/admin/updates/check", {})
        self.assertIn("update available", h.page)
        h.updates_post(SESS, "/admin/updates/dismiss", {})
        self.assertIn("You skipped this release", h.page)
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            h.updates_post(SESS, "/admin/updates/check", {})
        self.assertIn("reach GitHub", h.page)

    def test_unknown_action_is_404(self):
        h = _H()
        h.not_found = lambda: h.sent.append({"code": 404, "body": b""})
        h.updates_post(SESS, "/admin/updates/whatever", {})
        self.assertEqual(h.sent[-1]["code"], 404)

    def test_the_header_chip_is_for_admins_only_and_only_when_there_is_something_new(self):
        h = _H()
        self.assertNotIn("update-chip", h._app_header(SESS, "<b>left</b>"))
        _seed(_rel("v99.0.0"))
        chip = h._app_header(SESS, "<b>left</b>")
        self.assertIn("update-chip", chip)
        self.assertIn("href='/settings?tab=updates'", chip)
        self.assertIn("Nori v99.0.0 is available", chip)
        self.assertNotIn("update-chip", h._app_header(MEM, "<b>left</b>"), "a member never sees it")
        updates.dismiss(WS)
        self.assertNotIn("update-chip", h._app_header(SESS, "<b>left</b>"))

    def test_updates_is_a_settings_tab_for_admins_only(self):
        tabs = lambda sess: [t for _g, items in server.Handler._settings_groups(sess) for t, _l in items]  # noqa: E731
        self.assertIn("updates", tabs(SESS))
        self.assertNotIn("updates", tabs(MEM))


class Release(unittest.TestCase):
    def test_the_version_file_matches_what_a_tag_would_be(self):
        v = (ROOT / "VERSION").read_text().strip()
        self.assertIsNotNone(updates.parse_version(v), v)
        self.assertFalse(v.startswith("v"), "VERSION is the bare number; the tag is v + VERSION")

    def test_the_version_file_ships_in_the_image(self):
        ignored = (ROOT / ".dockerignore").read_text()
        self.assertNotRegex(ignored, r"(?m)^/?VERSION\s*$")


if __name__ == "__main__":
    unittest.main()

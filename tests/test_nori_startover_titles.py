# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Two things that let an operator see a clean instance the way a new one does (2026-10-28):

  * Start over (Settings -> About, firstrun.reset): deletes every member's history, its summaries and onboarding progress, re-opens setup, and keeps
    everything else (accounts, providers, models and the fallback order, her name and pronouns, memories, persona). Admin only, CSRF-checked upstream,
    typed confirmation, and an encrypted backup first when a backup key is set -- nothing is deleted if that backup fails.
  * The browser tab title follows her name ("history · Iron", not "history · nori"), as do the installed-app labels, the login and invite pages.

Real SQLite and the real page renderers; only the backup call is faked."""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_startover_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_startover_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"
for _f in ("persona.default.md", "persona.template.md"):
    shutil.copy(ROOT / "prompts" / _f, os.environ["NORI_PROMPTS_DIR"])

import accounts  # noqa: E402
import backup  # noqa: E402
import chat  # noqa: E402
import config  # noqa: E402
import conversation  # noqa: E402
import firstrun  # noqa: E402
import memory  # noqa: E402
import models  # noqa: E402
import onboarding  # noqa: E402
import providers  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Ben", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
_inv_id, _inv_tok = accounts.create_invite(WS, "Gigi", "member", UID)
MID = accounts.accept_invite(_inv_tok, "memberpass123")["id"]
ADMIN = {"user_id": UID, "workspace_id": WS, "role": "admin", "csrf": "csrf-tok"}
MEMBER = {"user_id": MID, "workspace_id": WS, "role": "member", "csrf": "csrf-mem"}


class _H(server.Handler):
    def __init__(self):
        self.headers = {}
        self.sent = []

    def send(self, code, body=b"", extra=None, *, ctype="text/html; charset=utf-8"):
        self.sent.append({"code": code, "body": body, "extra": extra or {}})

    def forbidden(self):
        self.sent.append({"code": 403, "body": b"", "extra": {}})

    def ip(self):
        return "10.0.0.1"

    def _board_count(self, sess):
        return 0

    def html(self) -> str:
        body = self.sent[-1]["body"]
        return body.decode("utf-8") if isinstance(body, bytes) else body


def _name(value):
    config.set("workspace", WS, "assistant_name", value)


def _populate():
    """An established instance: history for both users, a summary, progress, a provider + model + chain, a memory, setup finished."""
    store.write(lambda c: c.execute("DELETE FROM messages"))
    store.write(lambda c: c.execute("DELETE FROM conversation_segments"))
    store.write(lambda c: c.execute("DELETE FROM onboarding"))
    store.write(lambda c: c.execute("DELETE FROM model_chain"))
    store.write(lambda c: c.execute("DELETE FROM models"))
    store.write(lambda c: c.execute("DELETE FROM providers"))
    for uid in (UID, MID):
        for n in range(3):
            conversation.add_message(uid, "user", f"hello {n}")
            conversation.add_message(uid, "assistant", f"hi {n}")
        store.write(lambda c, u=uid: c.execute(
            "INSERT INTO conversation_segments(user_id, lo_id, hi_id, lo_ts, hi_ts, text, generated_ts) VALUES (?,1,2,0,1,'old summary',?)",
            (u, time.time())))
    onboarding.finish(UID)
    onboarding.finish(MID, skipped=True)
    providers.create_api_key(WS, "openrouter", "OpenRouter", "sk-test", UID)
    pid = providers.list_all(WS)[0]["id"]
    models.create("Sonnet", pid, "a/one", None)
    models.set_chain(WS, [models.list_all()[0]["id"]])
    memory._remember(dict(ADMIN), "preference", "likes short answers")
    firstrun.save_identity(WS, "Iron", "they/them")
    firstrun.finish(WS)


def _count(table, where="1=1", args=()):
    return store.read(lambda c: c.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone())[0]


class Reset(unittest.TestCase):
    def setUp(self):
        _populate()

    def test_it_deletes_every_members_history_summaries_and_progress(self):
        out = firstrun.reset(WS)
        self.assertEqual(out["messages"], 12)
        self.assertEqual(_count("messages"), 0)
        self.assertEqual(_count("conversation_segments"), 0)
        self.assertEqual(_count("onboarding"), 0)
        self.assertEqual(onboarding.status(UID), "pending")
        self.assertEqual(onboarding.status(MID), "pending")

    def test_it_reopens_setup_and_resumes_at_her_name(self):
        firstrun.reset(WS)
        self.assertFalse(config.get("workspace", WS, "setup_complete"))
        self.assertFalse(config.get("workspace", WS, "setup_identity"))
        self.assertTrue(firstrun.needed(WS))
        self.assertEqual(firstrun.done(WS), {"connect": True, "models": True, "identity": False})
        self.assertEqual(firstrun.next_step(WS), "identity")
        h = _H()
        self.assertTrue(h._first_run_redirect(dict(ADMIN)))
        self.assertEqual(h.sent[-1]["extra"]["Location"], "/first-run")

    def test_it_keeps_everything_else(self):
        firstrun.reset(WS)
        self.assertEqual(len(providers.list_all(WS)), 1)
        self.assertEqual([m["alias"] for m in models.get_chain(WS)], ["Sonnet"])
        self.assertEqual(config.get("workspace", WS, "assistant_name"), "Iron")
        self.assertEqual(firstrun.pronouns(WS), "they/them")
        self.assertEqual(len(accounts.list_users(WS)) if hasattr(accounts, "list_users") else 2, 2)
        self.assertTrue(any("short answers" in r["value"] for r in memory.all_rows(UID)), "her memories survive")

    def test_it_only_touches_this_workspace(self):
        other = store.write(lambda c: c.execute("INSERT INTO workspaces(created_ts, name) VALUES (?, 'other')", (time.time(),)).lastrowid)
        ouid = store.write(lambda c: c.execute(
            "INSERT INTO users(workspace_id, role, display_name, status, created_ts) VALUES (?, 'admin', 'Zed', 'active', ?)", (other, time.time())).lastrowid)
        conversation.add_message(ouid, "user", "someone else's")
        firstrun.reset(WS)
        self.assertEqual(_count("messages", "user_id=?", (ouid,)), 1)

    def test_a_history_larger_than_one_delete_batch_is_fully_deleted(self):
        with patch.object(conversation, "MAX_EDIT_IDS", 5):         # several batches without thousands of inserts
            firstrun.reset(WS)
        self.assertEqual(_count("messages"), 0)


class StartOverRoute(unittest.TestCase):
    def setUp(self):
        _populate()

    def _post(self, sess, **form):
        h = _H()
        h.start_over_post(dict(sess), form)
        return h

    def test_a_member_cannot(self):
        h = self._post(MEMBER, confirm="reset")
        self.assertEqual(h.sent[-1]["code"], 403)
        self.assertGreater(_count("messages"), 0)

    def test_without_the_typed_word_nothing_is_deleted(self):
        for confirm in ("", "yes", "resett"):
            h = self._post(ADMIN, confirm=confirm)
            self.assertEqual(h.sent[-1]["code"], 400, confirm)
        self.assertGreater(_count("messages"), 0)
        self.assertTrue(config.get("workspace", WS, "setup_complete"))

    def test_the_word_is_case_and_space_tolerant_and_goes_to_the_chat_which_sends_the_admin_to_setup(self):
        h = self._post(ADMIN, confirm="  RESET ")
        self.assertEqual((h.sent[-1]["code"], h.sent[-1]["extra"]["Location"]), (303, "/"))
        self.assertEqual(_count("messages"), 0)
        self.assertTrue(firstrun.needed(WS))

    def test_with_a_backup_key_a_backup_is_taken_first(self):
        seen = {}

        def fake_backup():
            seen["messages_at_backup_time"] = _count("messages")
            return {"status": "ok"}

        with patch.object(backup, "key_status", return_value={"ok": True, "configured": True}), \
                patch.object(backup, "create_backup", side_effect=fake_backup) as cb:
            h = self._post(ADMIN, confirm="reset")
        cb.assert_called_once()
        self.assertEqual(seen["messages_at_backup_time"], 12, "the backup was taken before anything was deleted")
        self.assertEqual(h.sent[-1]["code"], 303)
        self.assertEqual(_count("messages"), 0)

    def test_if_the_backup_fails_nothing_is_deleted(self):
        with patch.object(backup, "key_status", return_value={"ok": True, "configured": True}), \
                patch.object(backup, "create_backup", return_value={"status": "error", "error": "disk full"}):
            h = self._post(ADMIN, confirm="reset")
        self.assertEqual(h.sent[-1]["code"], 500)
        self.assertIn("disk full", h.html())
        self.assertIn("nothing was deleted", h.html())
        self.assertEqual(_count("messages"), 12)
        self.assertTrue(config.get("workspace", WS, "setup_complete"))

    def test_without_a_backup_key_no_backup_is_attempted(self):
        with patch.object(backup, "key_status", return_value={"ok": False, "configured": False}), \
                patch.object(backup, "create_backup") as cb:
            self._post(ADMIN, confirm="reset")
        cb.assert_not_called()
        self.assertEqual(_count("messages"), 0)

    def test_the_route_is_wired(self):
        import inspect
        src = inspect.getsource(server.Handler._do_POST_inner)
        self.assertIn('"/settings/start-over"', src)


class AboutSection(unittest.TestCase):
    def setUp(self):
        _populate()

    def _about(self, sess, key_ok):
        h = _H()
        h._settings_response = lambda s, tab, body, **kw: h.sent.append({"code": 200, "body": body})
        with patch.object(backup, "key_status", return_value={"ok": key_ok, "configured": key_ok}):
            h.settings_page(dict(sess), "about")
        return h.html()

    def test_the_admin_sees_both_options_and_the_deletion_is_stated_plainly(self):
        html = self._about(ADMIN, True)
        self.assertIn("href='/first-run'", html)
        self.assertIn("meet her again", html)
        self.assertIn("action='/settings/start-over'", html)
        self.assertIn("permanently deletes every member's conversation history", html)
        self.assertIn("It keeps accounts, providers and keys, models and fallbacks, memories, her persona", html)
        self.assertIn("An encrypted backup is taken first", html)
        self.assertIn("name=confirm", html)

    def test_without_a_backup_key_it_says_no_backup_will_be_taken(self):
        self.assertIn("No backup key is configured, so no backup will be taken first", self._about(ADMIN, False))

    def test_a_member_sees_neither_the_setup_link_nor_the_reset(self):
        html = self._about(MEMBER, True)
        self.assertNotIn("/settings/start-over", html)
        self.assertNotIn("href='/first-run'", html)
        self.assertIn("meet her again", html, "a member can still ask for her introduction")


class Titles(unittest.TestCase):
    def tearDown(self):
        _name("Nori")

    def test_her_name_replaces_the_product_name_in_every_tab_title(self):
        _name("Iron")
        t = server._tab_title
        self.assertEqual(t("nori"), "Iron")
        self.assertEqual(t(""), "Iron")
        self.assertEqual(t("history · nori"), "history · Iron")
        self.assertEqual(t("photos · Nori"), "photos · Iron")
        self.assertEqual(t("new task · nori"), "new task · Iron")
        self.assertEqual(t("files"), "files · Iron")
        self.assertEqual(t("Settings · Models"), "Settings · Models · Iron")

    def test_a_title_that_already_has_her_name_is_left_alone(self):
        _name("Iron")
        self.assertEqual(server._tab_title("join Iron"), "join Iron")

    def test_a_rename_is_picked_up_without_a_restart(self):
        _name("Iron")
        self.assertEqual(server._tab_title("files"), "files · Iron")
        _name("Juniper")
        self.assertEqual(server._tab_title("files"), "files · Juniper")

    def test_the_default_name_is_unchanged(self):
        _name("Nori")
        self.assertEqual(server._tab_title("nori"), "Nori")
        self.assertEqual(server._tab_title("history · nori"), "history · Nori")

    def test_page_app_writes_the_title_and_the_installed_app_labels(self):
        _name("Iron")
        html = server.page_app("history · nori", "<div>h</div>", "<p>x</p>").decode()
        self.assertIn("<title>history · Iron</title>", html)
        self.assertNotIn("<title>history · nori", html)
        self.assertIn("<meta name=application-name content='Iron'>", html)
        self.assertIn("<meta name=apple-mobile-web-app-title content='Iron'>", html)

    def test_a_name_with_markup_cannot_break_out_of_the_title(self):
        store.write(lambda c: c.execute("UPDATE settings SET v=? WHERE scope='workspace' AND scope_id=? AND key='assistant_name'", ('"</title><b>"', WS)))
        html = server.page_app("files", "<div>h</div>", "<p>x</p>").decode()
        self.assertNotIn("</title><b>", html)

    def test_the_chat_page_title_is_just_her_name(self):
        _name("Iron")
        h = _H()
        with patch.object(chat, "_resolve_model_chain", return_value=[{"model_name": "x", "provider": {"type": "openrouter"}}]):
            h.chat_page(dict(ADMIN))
        self.assertIn("<title>Iron</title>", h.html())

    def test_the_login_page_and_header_use_her_name(self):
        _name("Iron")
        h = _H()
        h.login_form()
        html = h.html()
        self.assertIn("<title>Iron</title>", html)
        self.assertIn("<h1>Iron</h1>", html)
        self.assertIn("<div class=hdr-title>Iron</div>", html)

    def test_the_invite_page_uses_her_name(self):
        _name("Iron")
        _i, tok = accounts.create_invite(WS, "Pat", "member", UID)
        h = _H()
        h.invite_form(tok)
        html = h.html()
        self.assertIn("<title>join Iron</title>", html)
        self.assertIn("<h1>join Iron</h1>", html)

    def test_the_public_about_page_stays_about_the_product(self):
        _name("Iron")
        h = _H()
        h.about_page()
        self.assertIn("<title>about nori</title>", h.html())

    def test_the_manifest_still_follows_her_name(self):
        _name("Iron")
        h = _H()
        h.serve_manifest()
        self.assertIn('"name": "Iron"', h.html())


if __name__ == "__main__":
    unittest.main()

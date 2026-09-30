# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Offline settings/navigation checks using real handlers and synthetic data."""
import ast
import html
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import urllib.parse

from test_nori_chat import ROOT, definitions

ADMIN_TABS = ("household", "peers", "subagents", "mcp", "tools", "avatars", "persona")
PERSONAL_TABS = ("pings", "notify", "memory", "accounts")


# Stand-ins for two real modules do_GET/settings_page touch on every call,
# not just on the paths these tests exercise -- found live (2026-09-18)
# when fixing this file's own rot: server.py's do_GET wraps every request
# in a real `timing` turn, and settings_page's per-tab dispatch dict
# constructs bound references to EVERY admin tab's own *_admin_form
# method (and _peer_messages_panel always queries `store` for a peer's
# message log) regardless of which single tab was actually requested.
# Neither `timing` nor `store` were ever in this file's synthetic env, so
# any test that reached those lines was one `NameError`/`TypeError` away
# from failing -- not exercised until the *_admin_form whitelist gap
# below was fixed and execution actually got that far.
class _EmptyCursor:
    def fetchone(self):
        return {"n": 0}

    def fetchall(self):
        return []


class _EmptyConn:
    def execute(self, *a, **k):
        return _EmptyCursor()


def _fake_store():
    return SimpleNamespace(read=lambda fn: fn(_EmptyConn()))


class _NullStage:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _NullTurn:
    turn_id = None

    def stage(self, name, **extra):
        return _NullStage()

    def finish(self):
        pass


_NULL_TURN = _NullTurn()


def _fake_timing():
    return SimpleNamespace(start_anywhere=lambda *a, **k: _NULL_TURN, NULL_TURN=_NULL_TURN)


def handler(role="admin"):
    sess = dict(role=role, user_id=1, workspace_id=1, csrf="test-csrf")
    env = dict(esc=html.escape, json=json, time=time, urllib=urllib, PWA_HEAD="", PWA_JS="",
               AVATAR_DIR=Path("avatars"), timing=_fake_timing(), store=_fake_store(),
               accounts=SimpleNamespace(any_users_exist=lambda: True, get_session=lambda _: sess,
                                        get_workspace=lambda _: {"name": "Test household"},
                                        list_users=lambda _: [dict(display_name="Test user", role=role, status="active")],
                                        create_invite=Mock(return_value=(2, "test-invite"))),
               config=SimpleNamespace(get=lambda *a: 0),
               # signal_registry() backs the "pings" tab's derived per-signal
               # toggles (2026-09-26) -- empty here is fine, this harness
               # doesn't need a real signal to render, just to not NameError.
               scheduler=SimpleNamespace(signal_registry=lambda: []),
               persona_admin=SimpleNamespace(render=lambda *a, **k: "<p>persona tab</p>", ACTIONS=()),
               # settings_page's notify tab reads this for the timezone
               # field's own placeholder value (2026-09-18).
               usertime=SimpleNamespace(zone_name=lambda uid: "America/New_York",
                                        is_valid_zone=lambda tz: True),
               emotion=SimpleNamespace(STATES=("neutral",)),
               # subagents_admin_form reads these four constants and calls
               # jobs.running_jobs()/recently_interrupted() and
               # models.list_enabled() unconditionally, not just when its
               # roster is non-empty -- real values from sub_agents.py, not
               # arbitrary, so a limit-range assertion here would still mean
               # something.
               sub_agents=SimpleNamespace(list_all=lambda: [], TOOL_CALL_LIMIT_MAX=1000,
                                          TOOL_BYTE_LIMIT_MIN=10_000, TOOL_BYTE_LIMIT_MAX=50_000_000,
                                          TOOL_BYTE_LIMIT_DEFAULT=2_000_000),
               jobs=SimpleNamespace(running_jobs=lambda: [], recently_interrupted=lambda: []),
               models=SimpleNamespace(list_enabled=lambda: [], list_all=lambda: []),
               providers=SimpleNamespace(list_all=lambda _: [], TYPES={}),
               mcp_servers=SimpleNamespace(list_servers=lambda _: []),
               peers=SimpleNamespace(list_peers=lambda _: []),
               diagnostics=SimpleNamespace(events=lambda _n: []),
               tool_builder=SimpleNamespace(list_drafts=lambda: []),
               connected_accounts=SimpleNamespace(list_for_user=lambda _: {}),
               memory=SimpleNamespace(removal_candidates=lambda _: [], all_rows=lambda _: [], TYPES=()))
    # info_tip is a plain module-level helper (not a Handler method) that
    # settings_page's chatvoice/notify/household tabs and subagents_admin_form
    # all call directly -- added in the same design pass that split Pings
    # into three tabs, never added here.
    definitions("server.py", {"BASE_CSS", "APP_JS", "KEYBOARD_JS", "NOTIFY_STATUS_JS", "page_app", "info_tip"}, env)
    tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
    cls.bases = []
    # settings_page builds its admin_pages dispatch dict unconditionally on
    # EVERY call (see server.py), binding all of these methods even
    # when the requested tab is something else entirely (e.g. "household")
    # -- so all of them have to exist on this synthetic class for ANY
    # settings_page call to avoid an AttributeError, not just the tabs a
    # given test happens to loop over. _settings_rail/_settings_shell/
    # settings_menu_page (2026-09-19, nav rebuild) are the new shared
    # helpers _settings_response itself now calls on every render --
    # same reasoning, added here so calling it doesn't AttributeError.
    # _do_GET_inner is do_GET's own
    # routing body (split out for the shared timing wrapper); do_GET can't
    # do anything real without it. backups_admin_form (2026-09-18) and
    # integration_health_admin_form (2026-09-19) were both added to this
    # dict the same way every other admin tab was -- referencing either
    # needs the method to EXIST here, not to actually render (no test
    # below puts "backups" or "health" in ADMIN_TABS, so neither one's
    # real body -- which reads the real backup/oauth/integration_health
    # modules -- ever actually runs in this harness).
    cls.body = [n for n in cls.body if getattr(n, "name", None) in {
        "do_GET", "_do_GET_inner", "_hdr_menu", "_hdr_back", "_app_header", "_settings_groups",
        "_settings_rail", "_settings_shell", "settings_menu_page",
        "_settings_response", "settings_page", "_household_members", "invite_admin_form", "invite_admin_post",
        "subagents_admin_form", "mcp_admin_form", "peers_admin_form", "tools_admin_form",
        "avatars_admin_page", "models_admin_form", "webtools_admin_form", "homeassistant_admin_form",
        "context_admin_form", "persona_admin_form", "persona_admin_post", "persona_preview_get", "context_preview_get", "context_admin_action", "context_admin_post", "media_admin_form", "backups_admin_form", "integration_health_admin_form",
        "_diagnostics_html", "_peer_messages_panel", "_fmt_peer_ts", "_peer_msg_preview", "peers_approval_post"}]
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "server.py", "exec"), env)
    instance = env["Handler"]()
    instance.send = Mock()
    instance.forbidden = lambda: instance.send(403, b"Forbidden")
    instance.token = lambda: "test-session"
    instance._find_avatar_file = lambda _: (None, None)
    return instance, sess, env


class NavigationTests(unittest.TestCase):
    def test_main_menu_has_only_daily_destinations(self):
        # 6, not 5: Chat/Inventory/Meals/Trackers/Files/Settings -- Trackers
        # became its own first-class daily destination (moved off Settings)
        # before this test was last updated.
        for role in ("admin", "member"):
            h, sess, _ = handler(role)
            menu = h._hdr_menu(sess)
            self.assertEqual(menu.count("<a "), 6)
            self.assertNotIn("/peers", menu)
            self.assertNotIn("/admin/", menu)

    def test_every_settings_tab_renders_with_one_selected_tab(self):
        for tab in ADMIN_TABS + PERSONAL_TABS:
            with self.subTest(tab=tab):
                h, sess, _ = handler()
                h.settings_page(sess, tab)
                status, body = h.send.call_args.args
                self.assertEqual(status, 200)
                text = body.decode()
                self.assertEqual(text.count("aria-current=page"), 1)
                self.assertIn(f"aria-current=page href='/settings?tab={tab}'", text)
                self.assertIn("<div class=hdr-title>Settings</div>", text)

    def test_bare_settings_shows_the_menu_not_a_default_tab(self):
        # 2026-09-19 nav rebuild: bare /settings used to silently default
        # to "pings" (settings_page's own q.get("tab", ["pings"])[0]) --
        # now it's a real menu screen, and do_GET is the thing that has
        # to make that choice, not settings_page itself (unchanged).
        h, sess, _ = handler()
        h.path = "/settings"
        h.do_GET()
        status, body = h.send.call_args.args
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn("<title>Settings</title>", text)
        self.assertNotIn("aria-current=page", text)  # nothing is "active" on the menu screen
        self.assertIn("settings-group-label", text)
        self.assertIn("Pings", text)  # a real tab row, from the real grouping
        # The CSS rules for settings-shell--active live in every page's own
        # <style> block regardless -- assert the actual HTML class
        # attribute, not just the substring's presence anywhere at all.
        self.assertIn("class='settings-shell'>", text)
        self.assertNotIn("class='settings-shell settings-shell--active'", text)

    def test_settings_tab_detail_has_a_back_link_and_active_shell_class(self):
        h, sess, _ = handler()
        h.path = "/settings?tab=pings"
        h.do_GET()
        text = h.send.call_args.args[1].decode()
        self.assertIn("class='settings-shell settings-shell--active'", text)
        self.assertIn("<a class=settings-back href='/settings'>", text)

    def test_no_horizontal_scrolling_nav_strip_survives_in_the_rail(self):
        # The old top nav rendered every group as a .settings-tabs row
        # (overflow-x:auto) -- confirms the rebuild actually replaced it,
        # not just added the new markup alongside the old.
        h, sess, _ = handler()
        h.settings_page(sess, "pings")
        text = h.send.call_args.args[1].decode()
        self.assertNotIn("settings-nav-group", text)
        self.assertNotIn("class=settings-tabs", text)

    def test_members_cannot_open_admin_settings_or_legacy_pages(self):
        for tab in ADMIN_TABS:
            h, sess, _ = handler("member")
            if tab != "peers":
                h.settings_page(sess, tab)
                self.assertEqual(h.send.call_args.args[0], 403)
            h.path = "/admin/" + ("invite" if tab == "household" else tab)
            h.do_GET()
            self.assertEqual(h.send.call_args.args[0], 403)
        h, sess, _ = handler("member")
        h.settings_page(sess, "pings")
        text = h.send.call_args.args[1].decode()
        for tab in ADMIN_TABS:
            if tab != "peers":
                self.assertNotIn(f"href='/settings?tab={tab}'", text)

    def test_legacy_bookmarks_redirect_to_matching_settings_tab(self):
        for tab in ADMIN_TABS:
            h, _, _ = handler()
            h.path = "/admin/" + ("invite" if tab == "household" else tab)
            h.do_GET()
            h.send.assert_called_once_with(303, b"", {"Location": "/settings?tab=" + tab})

    def test_invite_success_and_validation_stay_in_household_settings(self):
        h, sess, env = handler()
        h.invite_admin_post(sess, {"display_name": ""})
        text = h.send.call_args.args[1].decode()
        self.assertIn("name is required", text)
        self.assertIn("aria-current=page href='/settings?tab=household'", text)
        env["accounts"].create_invite.assert_not_called()
        h.invite_admin_post(sess, {"display_name": "New member", "role": "member"})
        text = h.send.call_args.args[1].decode()
        self.assertIn("/invite/test-invite", text)
        self.assertIn("action='/settings/household'", text)
        self.assertIn("action='/admin/invite'", text)
        self.assertIn("name=csrf value='test-csrf'", text)

    def test_peer_messages_are_embedded_in_settings_without_exposing_admin_controls(self):
        for role in ("admin", "member"):
            h, sess, _ = handler(role)
            h.settings_page(sess, "peers")
            text = h.send.call_args.args[1].decode()
            self.assertIn("<title>Settings · Peer connections</title>", text)
            self.assertIn("<details class=peer-debug id=peer-debug>", text)
            self.assertIn("<summary>Peer messages (debug)</summary>", text)
            self.assertEqual("action='/admin/peers'" in text, role == "admin")
            h.path = "/peers?msg=approved"
            h.do_GET()
            self.assertEqual(h.send.call_args.args[2]["Location"], "/settings?tab=peers&msg=approved#peer-debug")

    def test_pending_approvals_open_debug_panel_and_return_to_settings(self):
        h, sess, env = handler("member")
        env["peers"].list_peers = lambda _: [dict(id=7, name="Test peer", purpose="Test")]
        env["peers"].list_pending_actions = lambda *a: [dict(
            id=3, status="pending", tool_name="remember", tool_args_json="{}", requested_ts=1, expires_ts=2)]
        env["peers"].resolve_pending_action = Mock(return_value={})
        h.settings_page(sess, "peers")
        text = h.send.call_args.args[1].decode()
        self.assertIn("id=peer-debug open", text)
        self.assertIn("action='/peers/approvals/3/approve'", text)
        self.assertIn("name=csrf value='test-csrf'", text)
        h.peers_approval_post(sess, "3", "approve")
        self.assertEqual(h.send.call_args.args[2]["Location"], "/settings?tab=peers&msg=approved#peer-debug")
        env["peers"].resolve_pending_action.assert_called_once_with(sess, 3, approve=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--fixtures":
        pages = {}
        for role in ("admin", "member"):
            for tab in PERSONAL_TABS + (ADMIN_TABS if role == "admin" else ("peers",)):
                h, sess, _ = handler(role)
                h.settings_page(sess, tab)
                pages[f"/{role}/settings?tab={tab}"] = h.send.call_args.args[1].decode()
        Path(sys.argv[2]).write_text(json.dumps(pages), encoding="utf-8")
    else:
        unittest.main()

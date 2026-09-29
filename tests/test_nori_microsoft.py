# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Microsoft (Outlook/OneDrive/SharePoint) build, 2026-09-18: two Azure
app registrations, four widened/new Outlook-family provider entries, and
the real Outlook fixes (create_calendar_event's scope, the /calendarview
switch, draft/label parity with Gmail) -- deterministic, no live Graph
call possible (no Outlook account has ever been connected), so this
verifies request SHAPE (URL, method, body) via a monkeypatched
connected_accounts.authed_request, exactly the same "reviewed, not run"
honesty this whole connector layer is held to.
"""
import os
import sys
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_microsoft_")
os.environ["NORI_DATA_DIR"] = _SCRATCH

import connected_accounts  # noqa: E402
import contacts  # noqa: E402
import drive  # noqa: E402
import email_calendar  # noqa: E402
import oauth  # noqa: E402
import sharepoint  # noqa: E402
import tools  # noqa: E402

_SESS = {"user_id": 1, "workspace_id": 1, "role": "member"}


def _ok(data):
    return {"ok": True, "data": data}


class OAuthProvidersTests(unittest.TestCase):
    def test_outlook_scope_widened_to_readwrite(self):
        p = oauth.PROVIDERS["outlook"]
        self.assertIn("Mail.ReadWrite", p["scope"])
        self.assertIn("Calendars.ReadWrite", p["scope"])
        self.assertNotIn("Mail.Send", p["scope"])
        self.assertEqual(p["client_id_env"], "NORI_OUTLOOK_CLIENT_ID")

    def test_new_work_tenant_providers_share_the_work_credential_pair(self):
        for name in ("outlook_contacts", "onedrive_work", "sharepoint"):
            p = oauth.PROVIDERS[name]
            self.assertEqual(p["client_id_env"], "NORI_OUTLOOK_CLIENT_ID", name)
            self.assertEqual(p["client_secret_env"], "NORI_OUTLOOK_CLIENT_SECRET", name)
            self.assertIn("/common/", p["auth_url"], name)

    def test_onedrive_personal_uses_its_own_app_and_consumers_endpoint(self):
        p = oauth.PROVIDERS["onedrive_personal"]
        self.assertEqual(p["client_id_env"], "NORI_OUTLOOK_PERSONAL_CLIENT_ID")
        self.assertEqual(p["client_secret_env"], "NORI_OUTLOOK_PERSONAL_CLIENT_SECRET")
        self.assertIn("/consumers/", p["auth_url"])
        self.assertIn("/consumers/", p["token_url"])

    def test_sharepoint_requests_one_superset_scope_not_two(self):
        scope = oauth.PROVIDERS["sharepoint"]["scope"]
        self.assertIn("Sites.ReadWrite.All", scope)
        self.assertNotIn("Sites.Read.All", scope)  # ReadWrite.All is a strict superset -- not both
        self.assertNotIn("Sites.Selected", scope)  # his explicit choice: all sites, not a per-site allowlist

    def test_connected_accounts_providers_include_every_new_key(self):
        for name in ("outlook", "outlook_contacts", "onedrive_work", "onedrive_personal", "sharepoint"):
            self.assertIn(name, connected_accounts.PROVIDERS)
            self.assertIn(name, oauth.PROVIDERS)


class CalendarViewFixTests(unittest.TestCase):
    def test_outlook_calendar_query_uses_calendarview_with_a_bounded_window(self):
        captured = {}

        def fake_authed_request(user_id, provider, url, **kw):
            captured["url"] = url
            return _ok({"value": []})

        with patch.object(connected_accounts, "authed_request", side_effect=fake_authed_request):
            result = email_calendar._list_calendar_events_impl(_SESS, provider="outlook")
        self.assertNotIn("error", result)
        self.assertIn("/me/calendarview", captured["url"])
        self.assertIn("startDateTime=", captured["url"])
        self.assertIn("endDateTime=", captured["url"])
        self.assertNotIn("/me/events?", captured["url"])  # the old, never-time-bounded query

    def test_create_calendar_event_still_posts_to_events_not_calendarview(self):
        # Creation is a genuinely different endpoint from listing -- this
        # guards against "fixing" the wrong function by accident.
        captured = {}

        def fake_authed_request(user_id, provider, url, **kw):
            captured["url"], captured["method"] = url, kw.get("method")
            return _ok({"id": "evt1"})

        with patch.object(connected_accounts, "authed_request", side_effect=fake_authed_request):
            result = email_calendar._create_calendar_event_impl(
                _SESS, "outlook", "Dentist", "2026-10-01T09:00:00Z", "2026-10-01T10:00:00Z")
        self.assertEqual(result.get("event_id"), "evt1")
        self.assertTrue(captured["url"].endswith("/me/events"))
        self.assertEqual(captured["method"], "POST")


class DraftAndLabelParityTests(unittest.TestCase):
    def test_create_draft_outlook_posts_a_message_and_never_sends(self):
        captured = {}

        def fake_authed_request(user_id, provider, url, **kw):
            captured["url"], captured["method"], captured["body"] = url, kw.get("method"), kw.get("body")
            return _ok({"id": "draft1"})

        with patch.object(connected_accounts, "authed_request", side_effect=fake_authed_request):
            result = email_calendar._create_draft_impl(
                _SESS, "ben@example.com", "hi", "body text", provider="outlook")
        self.assertEqual(result["draft_id"], "draft1")
        self.assertIn("Outlook", result["note"])
        self.assertIn("not sent", result["note"])
        self.assertTrue(captured["url"].endswith("/me/messages"))
        self.assertEqual(captured["method"], "POST")
        self.assertNotIn("send", captured["url"].lower())

    def test_list_email_labels_outlook_reads_master_categories(self):
        def fake_authed_request(user_id, provider, url, **kw):
            self.assertIn("masterCategories", url)
            return _ok({"value": [{"displayName": "Red"}, {"displayName": "Follow up"}]})

        with patch.object(connected_accounts, "authed_request", side_effect=fake_authed_request):
            result = email_calendar._list_email_labels_impl(_SESS, provider="outlook")
        names = {lbl["name"] for lbl in result["labels"]}
        self.assertEqual(names, {"Red", "Follow up"})
        # Outlook categories have no separate opaque id -- name IS the id.
        self.assertEqual(result["labels"][0]["id"], result["labels"][0]["name"])

    def test_modify_email_labels_outlook_merges_categories_not_overwrites(self):
        calls = []

        def fake_authed_request(user_id, provider, url, **kw):
            calls.append((url, kw.get("method"), kw.get("body")))
            if url.endswith("masterCategories"):
                return _ok({"value": [{"displayName": "A"}, {"displayName": "B"}, {"displayName": "C"}]})
            if "$select=categories" in url:
                return _ok({"categories": ["A", "B"]})
            return _ok({"id": "msg1"})

        with patch.object(connected_accounts, "authed_request", side_effect=fake_authed_request):
            result = email_calendar._modify_email_labels_impl(
                _SESS, "msg1", add_labels=["C"], remove_labels=["A"], provider="outlook")
        self.assertTrue(result.get("ok"))
        patch_call = [c for c in calls if c[1] == "PATCH"]
        self.assertEqual(len(patch_call), 1)
        _, method, body = patch_call[0]
        self.assertEqual(sorted(body["categories"]), ["B", "C"])  # existing(A,B) - remove(A) + add(C)


class OutlookContactsTests(unittest.TestCase):
    def test_list_contacts_outlook_shapes_rows(self):
        def fake_authed_request(user_id, provider, url, **kw):
            self.assertEqual(provider, "outlook_contacts")
            return _ok({"value": [{"id": "c1", "displayName": "Ana", "emailAddresses":
                                   [{"address": "ana@example.com"}], "mobilePhone": "555-1234"}]})

        with patch.object(connected_accounts, "authed_request", side_effect=fake_authed_request):
            result = contacts._list_contacts_impl(_SESS, provider="outlook_contacts")
        row = result["contacts"][0]
        self.assertEqual(row["name"], "Ana")
        self.assertEqual(row["emails"], ["ana@example.com"])
        self.assertIn("555-1234", row["phones"])

    def test_search_contacts_outlook_uses_filter_not_search(self):
        captured = {}

        def fake_authed_request(user_id, provider, url, **kw):
            captured["url"] = url
            return _ok({"value": []})

        with patch.object(connected_accounts, "authed_request", side_effect=fake_authed_request):
            contacts._search_contacts_impl(_SESS, "O'Brien", provider="outlook_contacts")
        decoded = urllib.parse.unquote(captured["url"])
        self.assertIn("$filter=", decoded)
        self.assertIn("startswith(displayName,'O''Brien')", decoded)  # doubled quote: real OData escaping

    def test_invalid_provider_rejected(self):
        result = contacts._list_contacts_impl(_SESS, provider="not_a_real_provider")
        self.assertIn("error", result)


class OneDriveTests(unittest.TestCase):
    def test_list_files_onedrive_root_listing(self):
        def fake_authed_request(user_id, provider, url, **kw):
            self.assertEqual(provider, "onedrive_work")
            self.assertIn("root/children", url)
            return _ok({"value": [{"id": "f1", "name": "notes.txt", "lastModifiedDateTime": "t",
                                   "file": {"mimeType": "text/plain"}},
                                  {"id": "f2", "name": "Photos", "folder": {}}]})

        with patch.object(connected_accounts, "authed_request", side_effect=fake_authed_request):
            result = drive._list_files_onedrive_impl(_SESS, provider="onedrive_work")
        by_name = {f["name"]: f for f in result["files"]}
        self.assertEqual(by_name["notes.txt"]["type"], "text/plain")
        self.assertEqual(by_name["Photos"]["type"], "folder")

    def test_list_files_onedrive_search_when_query_given(self):
        captured = {}

        def fake_authed_request(user_id, provider, url, **kw):
            captured["url"] = url
            return _ok({"value": []})

        with patch.object(connected_accounts, "authed_request", side_effect=fake_authed_request):
            drive._list_files_onedrive_impl(_SESS, query="budget", provider="onedrive_personal")
        self.assertIn("search(q=", captured["url"])

    def test_invalid_onedrive_provider_rejected(self):
        result = drive._list_files_onedrive_impl(_SESS, provider="onedrive")  # old, pre-split name
        self.assertIn("error", result)

    def test_upload_bytes_to_onedrive_rejects_oversized_file(self):
        big = b"x" * (5 * 1024 * 1024)
        result = drive.upload_bytes_to_onedrive(_SESS, name="big.bin", content=big, folder_id="f1")
        self.assertIn("error", result)
        self.assertIn("too large", result["error"])

    def test_upload_bytes_to_onedrive_refuses_name_collision(self):
        def fake_authed_request(user_id, provider, url, **kw):
            return _ok({"value": [{"id": "existing"}]})  # name already exists in that folder

        with patch.object(connected_accounts, "authed_request", side_effect=fake_authed_request):
            result = drive.upload_bytes_to_onedrive(_SESS, name="dup.txt", content=b"hi", folder_id="f1")
        self.assertIn("error", result)
        self.assertIn("already exists", result["error"])


class SharePointTests(unittest.TestCase):
    def test_list_sites_defaults_to_wildcard_search(self):
        captured = {}

        def fake_authed_request(user_id, provider, url, **kw):
            captured["url"] = url
            return _ok({"value": [{"id": "s1", "displayName": "Ops", "webUrl": "https://x/sites/ops"}]})

        with patch.object(connected_accounts, "authed_request", side_effect=fake_authed_request):
            result = sharepoint._list_sharepoint_sites_impl(_SESS)
        self.assertIn("search=*", urllib.parse.unquote(captured["url"]))
        self.assertEqual(result["sites"][0]["site_id"], "s1")

    def test_list_files_scopes_to_one_site(self):
        captured = {}

        def fake_authed_request(user_id, provider, url, **kw):
            captured["url"] = url
            return _ok({"value": []})

        with patch.object(connected_accounts, "authed_request", side_effect=fake_authed_request):
            sharepoint._list_sharepoint_files_impl(_SESS, site_id="site-123")
        self.assertIn("sites/site-123/drive/root/children", captured["url"])

    def test_upload_rejects_oversized_file(self):
        big = b"x" * (5 * 1024 * 1024)
        result = sharepoint.upload_bytes_to_sharepoint(
            _SESS, site_id="s1", name="big.bin", content=big, folder_id="f1")
        self.assertIn("error", result)


class ToolRegistrationTests(unittest.TestCase):
    """Every new/widened tool actually registered, with the provider it
    should offer and the peer gate this session already settled on for
    its risk class -- schema_for()/dispatch()'s own enforcement is
    exercised via active_schemas(), not just read off the dict, so a typo
    in owner_check wiring would show up here."""

    def test_new_tools_are_registered(self):
        for name in ("list_files_onedrive", "read_file_content_onedrive",
                    "list_sharepoint_sites", "list_sharepoint_files", "read_sharepoint_file_content"):
            self.assertIsNotNone(tools.schema_for(name), name)

    def test_onedrive_and_sharepoint_tools_use_peer_trust_gate(self):
        for name in ("list_files_onedrive", "read_file_content_onedrive",
                    "list_sharepoint_sites", "list_sharepoint_files", "read_sharepoint_file_content"):
            t = tools._REGISTRY[name]
            self.assertIs(t.owner_check, connected_accounts.peer_trust_gate, name)

    def test_outlook_contacts_tools_use_peer_trust_gate_same_as_google(self):
        for name in ("list_contacts", "search_contacts"):
            t = tools._REGISTRY[name]
            self.assertIs(t.owner_check, connected_accounts.peer_trust_gate, name)

    def test_email_tools_still_use_peer_blocked_after_outlook_widening(self):
        for name in ("list_emails", "triage_email", "create_draft", "list_email_labels", "modify_email_labels"):
            t = tools._REGISTRY[name]
            self.assertIs(t.owner_check, connected_accounts.peer_blocked, name)

    def test_create_draft_schema_now_offers_outlook(self):
        schema = tools.schema_for("create_draft")
        enum = schema["function"]["parameters"]["properties"]["provider"]["enum"]
        self.assertIn("outlook", enum)
        self.assertIn("gmail", enum)

    def test_active_schemas_member_session_sees_new_tools(self):
        sess = {"role": "member"}
        names = {s["function"]["name"] for s in tools.active_schemas(sess)}
        for n in ("list_files_onedrive", "list_sharepoint_sites", "list_contacts"):
            self.assertIn(n, names)


if __name__ == "__main__":
    unittest.main()

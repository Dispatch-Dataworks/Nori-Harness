# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""integration_health.py -- extends the PACI specification's §4.1 liveness pattern
to Home Assistant, Tavily, every connected Google/Microsoft account, and
every connected MCP server. Every real network call (connected_accounts.
authed_request, homeassistant._request, webtools.search, mcp_client.
list_tools) is mocked -- there's no live paired account/instance in this
harness -- but the classification logic, the not_configured/needs_
reconnect short-circuits, the transition-only logging, and the interval
gating are all real and unmocked, same discipline test_nori_paci_health.py
already established for the peer health check this extends."""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_SCRATCH = tempfile.mkdtemp(prefix="nori_test_integration_health_")
os.environ["NORI_DATA_DIR"] = _SCRATCH
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import connected_accounts  # noqa: E402
import homeassistant  # noqa: E402
import integration_health  # noqa: E402
import contacts  # noqa: E402
import drive  # noqa: E402
import email_calendar  # noqa: E402
import sharepoint  # noqa: E402
import mcp_servers  # noqa: E402
import store  # noqa: E402
import webtools  # noqa: E402

store.init()
_user = accounts.bootstrap_admin("Tester", "testpass123")
_SESS = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "admin"}


def _clear_env(*keys):
    for k in keys:
        os.environ.pop(k, None)


class ClassifyHttpTests(unittest.TestCase):
    def test_none_status_is_unreachable(self):
        self.assertEqual(integration_health._classify_http(None, ""), "unreachable")

    def test_401_is_auth_expired(self):
        self.assertEqual(integration_health._classify_http(401, "revoked"), "auth_expired")

    def test_429_is_rate_limited(self):
        self.assertEqual(integration_health._classify_http(429, "slow down"), "rate_limited")

    def test_403_with_disabled_wording_is_api_disabled(self):
        text = "Gmail API has not been used in project 123 before or it is disabled"
        self.assertEqual(integration_health._classify_http(403, text), "api_disabled")

    def test_403_generic_is_scope_missing(self):
        self.assertEqual(integration_health._classify_http(
            403, "Request had insufficient authentication scopes"), "scope_missing")

    def test_unrecognized_status_is_error(self):
        self.assertEqual(integration_health._classify_http(500, "server exploded"), "error")


class ProbeOAuthTests(unittest.TestCase):
    def tearDown(self):
        connected_accounts.disconnect(_user["id"], "gmail")

    def test_not_configured_without_env_credentials(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_env("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
            r = integration_health._probe_oauth(_user["id"], "gmail", "https://example.invalid/x")
        self.assertEqual(r["status"], "not_configured")

    def test_not_configured_when_never_connected(self):
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csecret"}):
            r = integration_health._probe_oauth(_user["id"], "gmail", "https://example.invalid/x")
        self.assertEqual(r["status"], "not_configured")

    def test_auth_expired_when_needs_reconnect_without_a_live_call(self):
        connected_accounts.start_pending(_user["id"], "gmail", "state1")
        connected_accounts.store_tokens(_user["id"], "gmail", "tok", "reftok", 3600)
        connected_accounts.mark_needs_reconnect(_user["id"], "gmail", "invalid_grant: token revoked")
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csecret"}), \
             patch.object(connected_accounts, "authed_request") as mock_call:
            r = integration_health._probe_oauth(_user["id"], "gmail", "https://example.invalid/x")
        mock_call.assert_not_called()
        self.assertEqual(r["status"], "auth_expired")
        self.assertIn("revoked", r["detail"])

    def test_healthy_when_probe_succeeds(self):
        connected_accounts.start_pending(_user["id"], "gmail", "state2")
        connected_accounts.store_tokens(_user["id"], "gmail", "tok", "reftok", 3600)
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csecret"}), \
             patch.object(connected_accounts, "authed_request", return_value={"ok": True, "data": {}}):
            r = integration_health._probe_oauth(_user["id"], "gmail", "https://example.invalid/x")
        self.assertEqual(r["status"], "healthy")

    def test_classified_failure_flows_through_from_authed_request(self):
        connected_accounts.start_pending(_user["id"], "gmail", "state3")
        connected_accounts.store_tokens(_user["id"], "gmail", "tok", "reftok", 3600)
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csecret"}), \
             patch.object(connected_accounts, "authed_request",
                          return_value={"ok": False, "error": "too many requests", "http_status": 429}):
            r = integration_health._probe_oauth(_user["id"], "gmail", "https://example.invalid/x")
        self.assertEqual(r["status"], "rate_limited")
        self.assertEqual(r["detail"], "too many requests")


class HomeAssistantCheckTests(unittest.TestCase):
    def test_not_configured(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_env("HOME_ASSISTANT_URL", "HOME_ASSISTANT_API_KEY")
            r = integration_health._check_home_assistant()
        self.assertEqual(r["status"], "not_configured")

    def test_healthy(self):
        with patch.dict(os.environ, {"HOME_ASSISTANT_URL": "http://example.invalid", "HOME_ASSISTANT_API_KEY": "k"}), \
             patch.object(homeassistant, "_request", return_value=(True, 200, {"message": "API running."})):
            r = integration_health._check_home_assistant()
        self.assertEqual(r["status"], "healthy")

    def test_auth_expired(self):
        with patch.dict(os.environ, {"HOME_ASSISTANT_URL": "http://example.invalid", "HOME_ASSISTANT_API_KEY": "k"}), \
             patch.object(homeassistant, "_request", return_value=(False, 401, "wrong or expired")):
            r = integration_health._check_home_assistant()
        self.assertEqual(r["status"], "auth_expired")

    def test_unreachable(self):
        with patch.dict(os.environ, {"HOME_ASSISTANT_URL": "http://example.invalid", "HOME_ASSISTANT_API_KEY": "k"}), \
             patch.object(homeassistant, "_request", return_value=(False, None, "couldn't reach Home Assistant")):
            r = integration_health._check_home_assistant()
        self.assertEqual(r["status"], "unreachable")

    def test_other_http_error_is_error_not_auth_expired(self):
        with patch.dict(os.environ, {"HOME_ASSISTANT_URL": "http://example.invalid", "HOME_ASSISTANT_API_KEY": "k"}), \
             patch.object(homeassistant, "_request", return_value=(False, 500, "HTTP 500: internal error")):
            r = integration_health._check_home_assistant()
        self.assertEqual(r["status"], "error")


class TavilyCheckTests(unittest.TestCase):
    def test_not_configured(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_env("TAVILY_API_KEY")
            r = integration_health._check_tavily(live=False)
        self.assertEqual(r["status"], "not_configured")

    def test_configured_non_live_spends_nothing_and_returns_none(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": "k"}), \
             patch.object(webtools, "search") as mock_search:
            r = integration_health._check_tavily(live=False)
        mock_search.assert_not_called()
        self.assertIsNone(r)

    def test_live_healthy(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": "k"}), \
             patch.object(webtools, "search", return_value={"ok": True}):
            r = integration_health._check_tavily(live=True)
        self.assertEqual(r["status"], "healthy")

    def test_live_auth_expired(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": "k"}), \
             patch.object(webtools, "search",
                          return_value={"ok": False, "reason": "web search failed (401): Unauthorized"}):
            r = integration_health._check_tavily(live=True)
        self.assertEqual(r["status"], "auth_expired")

    def test_live_rate_limited(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": "k"}), \
             patch.object(webtools, "search",
                          return_value={"ok": False, "reason": "web search failed (429): slow down"}):
            r = integration_health._check_tavily(live=True)
        self.assertEqual(r["status"], "rate_limited")

    def test_live_unreachable(self):
        with patch.dict(os.environ, {"TAVILY_API_KEY": "k"}), \
             patch.object(webtools, "search",
                          return_value={"ok": False,
                                       "reason": "web search failed: <urlopen error timed out>"}):
            r = integration_health._check_tavily(live=True)
        self.assertEqual(r["status"], "unreachable")


class McpServerHealthTests(unittest.TestCase):
    def _server(self, name="TestSrv"):
        result = mcp_servers.create_server(_SESS, scope="user", name=name,
                                           url="https://example.invalid/mcp", auth_type="none")
        self.assertTrue(result.get("ok"), result)
        return result["server_id"]

    def test_healthy(self):
        sid = self._server("Healthy")
        with patch.object(mcp_servers.mcp_client, "list_tools", return_value=[]):
            r = mcp_servers.check_health(sid)
        self.assertEqual(r["status"], "healthy")

    def test_auth_expired_via_401(self):
        sid = self._server("Auth401")
        with patch.object(mcp_servers.mcp_client, "list_tools",
                          side_effect=mcp_servers.mcp_client.MCPError("server returned HTTP 401")):
            r = mcp_servers.check_health(sid)
        self.assertEqual(r["status"], "auth_expired")

    def test_scope_missing_via_403(self):
        sid = self._server("Scope403")
        with patch.object(mcp_servers.mcp_client, "list_tools",
                          side_effect=mcp_servers.mcp_client.MCPError("server returned HTTP 403")):
            r = mcp_servers.check_health(sid)
        self.assertEqual(r["status"], "scope_missing")

    def test_rate_limited_via_429(self):
        sid = self._server("Rate429")
        with patch.object(mcp_servers.mcp_client, "list_tools",
                          side_effect=mcp_servers.mcp_client.MCPError("server returned HTTP 429")):
            r = mcp_servers.check_health(sid)
        self.assertEqual(r["status"], "rate_limited")

    def test_unreachable_via_network_error(self):
        sid = self._server("Unreachable")
        with patch.object(mcp_servers.mcp_client, "list_tools",
                          side_effect=mcp_servers.mcp_client.MCPError("could not reach server: refused")):
            r = mcp_servers.check_health(sid)
        self.assertEqual(r["status"], "unreachable")

    def test_disabled_connection_is_not_configured_without_a_live_call(self):
        sid = self._server("Disabled")
        mcp_servers.set_server_enabled(_SESS, sid, False)
        with patch.object(mcp_servers.mcp_client, "list_tools") as mock_call:
            r = mcp_servers.check_health(sid)
        mock_call.assert_not_called()
        self.assertEqual(r["status"], "not_configured")


class ExplainTests(unittest.TestCase):
    """explain (2026-09-19, a follow-up) is what Nori actually relays
    -- these tests exist to prove it distinguishes cases a bare status
    code collapses: two different not_configured reasons, and a live
    failure from a cached needs_reconnect one, each need their own
    specific, actionable text."""
    def tearDown(self):
        connected_accounts.disconnect(_user["id"], "gmail")

    def test_no_app_credentials_and_never_connected_read_differently(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_env("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
            no_creds = integration_health._probe_oauth(_user["id"], "gmail", "https://example.invalid/x")
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csecret"}):
            never_connected = integration_health._probe_oauth(_user["id"], "gmail", "https://example.invalid/x")
        self.assertEqual(no_creds["status"], "not_configured")
        self.assertEqual(never_connected["status"], "not_configured")
        self.assertNotEqual(no_creds["explain"], never_connected["explain"])
        self.assertIn("admin", no_creds["explain"].lower())
        self.assertIn("connect", never_connected["explain"].lower())

    def test_auth_expired_explain_names_reconnecting(self):
        connected_accounts.start_pending(_user["id"], "gmail", "s1")
        connected_accounts.store_tokens(_user["id"], "gmail", "tok", "reftok", 3600)
        connected_accounts.mark_needs_reconnect(_user["id"], "gmail", "invalid_grant")
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csecret"}):
            r = integration_health._probe_oauth(_user["id"], "gmail", "https://example.invalid/x")
        self.assertIn("reconnect", r["explain"].lower())

    def test_needs_reconnect_goes_through_the_shared_diagnosis_not_its_own_computation(self):
        """The unification itself (2026-09-27): this tool, the settings
        page, and the account_needs_reconnect ping signal must all read
        connected_accounts.reconnect_diagnosis() rather than each
        computing their own answer -- proven here by the Testing-status
        heuristic (Google, invalid_grant, connected 10 days ago) showing
        up in THIS tool's own explain text, not just the other two
        surfaces."""
        connected_accounts.start_pending(_user["id"], "gmail", "s1")
        connected_accounts.store_tokens(_user["id"], "gmail", "tok", "reftok", 3600)
        connected_accounts.mark_needs_reconnect(_user["id"], "gmail",
                                                "invalid_grant: Token has been expired or revoked.")
        import store
        backdated = time.time() - 10 * 86400
        store.write(lambda c: c.execute(
            "UPDATE connected_accounts SET connected_ts=? WHERE user_id=? AND provider=?",
            (backdated, _user["id"], "gmail")))
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csecret"}):
            r = integration_health._probe_oauth(_user["id"], "gmail", "https://example.invalid/x")
        self.assertIn("Testing", r["explain"])
        self.assertIn("Testing", r["detail"])

    def test_scope_missing_and_api_disabled_get_distinct_actionable_explains(self):
        connected_accounts.start_pending(_user["id"], "gmail", "s2")
        connected_accounts.store_tokens(_user["id"], "gmail", "tok", "reftok", 3600)
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csecret"}), \
             patch.object(connected_accounts, "authed_request",
                          return_value={"ok": False, "error": "insufficient scope", "http_status": 403}):
            scope_r = integration_health._probe_oauth(_user["id"], "gmail", "https://example.invalid/x")
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "csecret"}), \
             patch.object(connected_accounts, "authed_request",
                          return_value={"ok": False, "error": "API has not been used in project", "http_status": 403}):
            disabled_r = integration_health._probe_oauth(_user["id"], "gmail", "https://example.invalid/x")
        self.assertNotEqual(scope_r["explain"], disabled_r["explain"])
        self.assertIn("permission", scope_r["explain"].lower())
        self.assertIn("api", disabled_r["explain"].lower())

    def test_tavily_not_configured_names_the_missing_key(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_env("TAVILY_API_KEY")
            r = integration_health._check_tavily(live=False)
        self.assertIn("tavily", r["explain"].lower())
        self.assertIn("key", r["explain"].lower())

    def test_home_assistant_not_configured_names_env_vars(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_env("HOME_ASSISTANT_URL", "HOME_ASSISTANT_API_KEY")
            r = integration_health._check_home_assistant()
        self.assertIn(".env", r["explain"])

    def test_mcp_disabled_explain_names_the_settings_page(self):
        sid = mcp_servers.create_server(_SESS, scope="user", name="ExplainSrv",
                                        url="https://example.invalid/mcp", auth_type="none")["server_id"]
        mcp_servers.set_server_enabled(_SESS, sid, False)
        r = mcp_servers.check_health(sid)
        self.assertIn("MCP servers", r["explain"])

    def test_generic_explain_covers_every_state(self):
        for status in integration_health.STATES:
            text = integration_health._generic_explain("Widget", status, "some detail")
            self.assertTrue(text)


class ToolViewTests(unittest.TestCase):
    def test_trimmed_shape_has_no_raw_detail_or_timestamps(self):
        with patch.object(mcp_servers, "list_all_servers", return_value=[]):
            view = integration_health.tool_view()
        for key, entry in view.items():
            self.assertEqual(set(entry.keys()), {"label", "status", "explain"})

    def test_dispatch_returns_the_trimmed_view(self):
        import tools
        member_sess = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "member"}
        result = tools.dispatch("check_integration_health", {}, member_sess)
        entry = result["integrations"]["home_assistant"]
        self.assertEqual(set(entry.keys()), {"label", "status", "explain"})
        self.assertIsInstance(entry["explain"], str)
        self.assertGreater(len(entry["explain"]), 0)


class ExplainPersistenceTests(unittest.TestCase):
    def test_record_and_health_status_round_trip_explain(self):
        integration_health._record("persist_test", "healthy", "raw detail here", "short relayable explain")
        h = integration_health.health_status("persist_test")
        self.assertEqual(h["detail"], "raw detail here")
        self.assertEqual(h["explain"], "short relayable explain")

    def test_get_all_fills_a_generic_explain_for_a_never_checked_key(self):
        with patch.object(mcp_servers, "list_all_servers", return_value=[]):
            out = integration_health.get_all()
        # tavily may or may not have been checked by earlier tests in this
        # module -- assert the invariant that matters: every entry has a
        # non-empty explain, never blank.
        for key, entry in out.items():
            self.assertTrue(entry["explain"], f"{key} has an empty explain")


class RecordTransitionTests(unittest.TestCase):
    def test_logged_only_when_status_actually_changes(self):
        with patch("builtins.print") as mock_print:
            integration_health._record("transition_test", "healthy", "")
            integration_health._record("transition_test", "healthy", "")
            integration_health._record("transition_test", "unreachable", "network down")
        transition_lines = [c for c in mock_print.call_args_list if "->" in str(c)]
        self.assertEqual(len(transition_lines), 2)  # None->healthy, healthy->unreachable


class RunAllChecksTests(unittest.TestCase):
    def test_sweep_records_home_assistant_and_every_oauth_key(self):
        with patch.object(integration_health, "_check_home_assistant",
                          return_value={"status": "not_configured", "detail": "no HA"}), \
             patch.object(integration_health, "_check_tavily", return_value=None), \
             patch.object(integration_health, "_probe_oauth",
                          return_value={"status": "not_configured", "detail": "no creds"}), \
             patch.object(mcp_servers, "list_all_servers", return_value=[]):
            results = integration_health.run_all_checks(live_tavily=False)
        self.assertEqual(results["home_assistant"]["status"], "not_configured")
        for key, _, _ in integration_health._OAUTH_CHECKS:
            self.assertIn(key, results)
        self.assertEqual(integration_health.health_status("gmail")["status"], "not_configured")

    def test_non_live_sweep_never_overwrites_tavily_with_a_guess(self):
        integration_health._record("tavily", "healthy", "earlier live check")
        with patch.object(integration_health, "_check_home_assistant",
                          return_value={"status": "healthy", "detail": ""}), \
             patch.object(integration_health, "_check_tavily", return_value=None), \
             patch.object(integration_health, "_probe_oauth",
                          return_value={"status": "healthy", "detail": ""}), \
             patch.object(mcp_servers, "list_all_servers", return_value=[]):
            results = integration_health.run_all_checks(live_tavily=False)
        self.assertNotIn("tavily", results)
        self.assertEqual(integration_health.health_status("tavily")["status"], "healthy")

    def test_sweep_checks_every_connected_mcp_server(self):
        with patch.object(integration_health, "_check_home_assistant",
                          return_value={"status": "not_configured", "detail": ""}), \
             patch.object(integration_health, "_check_tavily", return_value=None), \
             patch.object(integration_health, "_probe_oauth",
                          return_value={"status": "not_configured", "detail": ""}), \
             patch.object(mcp_servers, "list_all_servers",
                          return_value=[{"id": 42, "name": "Nodrya", "enabled": 1}]), \
             patch.object(mcp_servers, "check_health",
                          return_value={"status": "healthy", "detail": "tools/list responded"}) as mock_check:
            results = integration_health.run_all_checks(live_tavily=False)
        mock_check.assert_called_once_with(42)
        self.assertEqual(results["mcp:42"]["status"], "healthy")


class GetAllTests(unittest.TestCase):
    def test_every_fixed_key_present_even_if_never_checked(self):
        with patch.object(mcp_servers, "list_all_servers", return_value=[]):
            out = integration_health.get_all()
        for key in integration_health.LABELS:
            self.assertIn(key, out)

    def test_mcp_rows_labeled_fresh_from_the_live_connection_list(self):
        with patch.object(mcp_servers, "list_all_servers", return_value=[{"id": 7, "name": "Nodrya", "enabled": 1}]):
            out = integration_health.get_all()
        self.assertEqual(out["mcp:7"]["label"], "MCP: Nodrya")
        self.assertEqual(out["mcp:7"]["status"], "unknown")


class TickTests(unittest.TestCase):
    def test_skips_when_recently_checked(self):
        integration_health._record("home_assistant", "healthy", "")
        with patch.object(integration_health, "run_all_checks") as mock_run:
            integration_health.tick()
        mock_run.assert_not_called()

    def test_runs_when_interval_elapsed(self):
        store.write(lambda c: c.execute(
            "INSERT INTO integration_health(key, last_status, last_checked_ts, last_detail, status_changed_ts) "
            "VALUES ('home_assistant','healthy',?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET last_checked_ts=excluded.last_checked_ts",
            (time.time() - 3600, "", time.time() - 3600)))
        with patch.object(integration_health, "run_all_checks") as mock_run:
            integration_health.tick()
        mock_run.assert_called_once_with(live_tavily=False)


def _set_status(key, status, age_s=0.0, detail="", explain=""):
    """Direct write into integration_health, same pattern
    TickTests.test_runs_when_interval_elapsed already uses -- sets
    status_changed_ts to `age_s` seconds in the past so a persistence
    threshold can be tested without waiting for real time to pass."""
    now = time.time()
    store.write(lambda c: c.execute(
        "INSERT INTO integration_health(key, last_status, last_checked_ts, last_detail, last_explain, "
        "status_changed_ts) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET last_status=excluded.last_status, "
        "last_checked_ts=excluded.last_checked_ts, last_detail=excluded.last_detail, "
        "last_explain=excluded.last_explain, status_changed_ts=excluded.status_changed_ts",
        (key, status, now, detail, explain, now - age_s)))


class PingSignalTests(unittest.TestCase):
    """The two proactive signals this module registers (2026-09-27):
    scheduler_signal_action_needed (auth_expired/scope_missing/
    api_disabled, minus the OAuth-auth_expired overlap connected_accounts'
    own account_needs_reconnect already covers) and
    scheduler_signal_degraded (error immediately; unreachable/rate_limited/
    unknown only once persisted past _PERSISTENT_THRESHOLD_S)."""

    def tearDown(self):
        for key in ("home_assistant", "tavily", "gmail", "google_calendar"):
            store.write(lambda c, k=key: c.execute("DELETE FROM integration_health WHERE key=?", (k,)))

    # -- action_needed --

    def test_action_needed_none_when_everything_healthy(self):
        _set_status("home_assistant", "healthy")
        self.assertIsNone(integration_health.scheduler_signal_action_needed(_user["id"]))

    def test_action_needed_names_home_assistant_auth_expired(self):
        _set_status("home_assistant", "auth_expired", explain="key looks wrong")
        message, dedup = integration_health.scheduler_signal_action_needed(_user["id"])
        self.assertIn("Home Assistant", message)
        self.assertEqual(dedup, "home_assistant")

    def test_action_needed_excludes_oauth_auth_expired_already_covered_elsewhere(self):
        _set_status("gmail", "auth_expired", explain="expired")
        self.assertIsNone(integration_health.scheduler_signal_action_needed(_user["id"]))

    def test_action_needed_includes_oauth_scope_missing_not_covered_elsewhere(self):
        _set_status("gmail", "scope_missing", explain="missing a permission")
        message, dedup = integration_health.scheduler_signal_action_needed(_user["id"])
        self.assertIn("Gmail", message)
        self.assertEqual(dedup, "gmail")

    def test_action_needed_two_broken_both_named_dedup_is_combined(self):
        _set_status("home_assistant", "auth_expired")
        _set_status("gmail", "api_disabled")
        message, dedup = integration_health.scheduler_signal_action_needed(_user["id"])
        self.assertIn("Home Assistant", message)
        self.assertIn("Gmail", message)
        self.assertEqual(dedup, "gmail,home_assistant")

    def test_action_needed_costs_no_network_call(self):
        _set_status("home_assistant", "auth_expired")
        with patch.object(connected_accounts, "authed_request") as mock_ca, \
             patch.object(homeassistant, "_request") as mock_ha:
            integration_health.scheduler_signal_action_needed(_user["id"])
        mock_ca.assert_not_called()
        mock_ha.assert_not_called()

    # -- degraded --

    def test_degraded_none_when_nothing_qualifies(self):
        _set_status("home_assistant", "healthy")
        self.assertIsNone(integration_health.scheduler_signal_degraded(_user["id"]))

    def test_degraded_error_included_immediately_no_threshold(self):
        _set_status("tavily", "error", age_s=1)
        message, dedup = integration_health.scheduler_signal_degraded(_user["id"])
        self.assertIn("Tavily", message)
        self.assertEqual(dedup, "tavily")

    def test_degraded_unreachable_excluded_before_the_threshold(self):
        _set_status("home_assistant", "unreachable", age_s=3600)  # 1h, well under 24h
        self.assertIsNone(integration_health.scheduler_signal_degraded(_user["id"]))

    def test_degraded_unreachable_included_after_the_threshold(self):
        _set_status("home_assistant", "unreachable", age_s=25 * 3600)
        message, dedup = integration_health.scheduler_signal_degraded(_user["id"])
        self.assertIn("Home Assistant", message)
        self.assertEqual(dedup, "home_assistant")

    def test_degraded_unknown_excluded_before_the_threshold(self):
        _set_status("tavily", "unknown", age_s=60)
        self.assertIsNone(integration_health.scheduler_signal_degraded(_user["id"]))

    def test_degraded_unknown_included_after_the_threshold(self):
        _set_status("tavily", "unknown", age_s=25 * 3600)
        message, dedup = integration_health.scheduler_signal_degraded(_user["id"])
        self.assertIn("Tavily", message)

    def test_degraded_costs_no_network_call(self):
        _set_status("tavily", "error")
        with patch.object(connected_accounts, "authed_request") as mock_ca, \
             patch.object(homeassistant, "_request") as mock_ha:
            integration_health.scheduler_signal_degraded(_user["id"])
        mock_ca.assert_not_called()
        mock_ha.assert_not_called()

    # -- both registered --

    def test_both_signals_registered_with_the_others(self):
        import scheduler
        keys = [s["key"] for s in scheduler.signal_registry()]
        self.assertIn("integration_needs_attention", keys)
        self.assertIn("integration_degraded", keys)


class ToolTests(unittest.TestCase):
    def test_registered_and_reads_cache_only_never_triggers_a_live_sweep(self):
        import tools
        schema = tools.schema_for("check_integration_health")
        self.assertIsNotNone(schema)
        member_sess = {"user_id": _user["id"], "workspace_id": _user["workspace_id"], "role": "member"}
        with patch.object(integration_health, "run_all_checks") as mock_run:
            result = tools.dispatch("check_integration_health", {}, member_sess)
        mock_run.assert_not_called()
        self.assertIn("integrations", result)
        self.assertIn("home_assistant", result["integrations"])


class ProbeEndpointsMatchRealToolsTests(unittest.TestCase):
    """2026-09-22 (the operator: "Google tokens in nori don't seem to be refreshing"): the real bug was google_calendar's and google_contacts' probes hitting standard 'identity' endpoints
    (calendarList, bare people/me) that need a BROADER scope than the app requests or her real tools ever use -- a false scope_missing that told him to reconnect something
    that, proven the same minute by her own list_calendar_events call, worked fine. Every _OAUTH_CHECKS entry must be a real endpoint of the module _REAL_TOOL_MODULES says
    owns it, so a probe can never again test a path the app will never exercise; and _REAL_TOOL_MODULES itself must cover every _OAUTH_CHECKS key, so a NEW probe added
    without wiring up this mapping fails loudly here instead of silently shipping unchecked."""

    def test_every_check_has_an_entry_in_the_real_tool_module_mapping(self):
        keys = {k for k, _p, _u in integration_health._OAUTH_CHECKS}
        self.assertEqual(keys, set(integration_health._REAL_TOOL_MODULES), "a probe was added or removed without updating _REAL_TOOL_MODULES to match")

    def test_every_probe_url_is_a_real_endpoint_of_its_own_module(self):
        for key, _provider, url in integration_health._OAUTH_CHECKS:
            mod = integration_health._REAL_TOOL_MODULES[key]
            base = url.split("?")[0]
            real = integration_health.real_endpoint_urls(mod)
            self.assertTrue(any(base.startswith(r) for r in real),
                            f"{key}: probe URL {base!r} is not one of {mod.__name__}'s own real endpoints {sorted(real)} -- "
                            f"it will test a path the app never exercises and can 403 on a scope the app never requested")

    def test_specifically_calendar_and_contacts_hit_the_same_url_the_real_tools_call(self):
        # the two that were actually wrong live: pin them to the literal constant the real tool uses, not just "some endpoint of the module"
        cal = next(u for k, _p, u in integration_health._OAUTH_CHECKS if k == "google_calendar")
        self.assertTrue(cal.startswith(email_calendar._GCAL_EVENTS_URL))
        con = next(u for k, _p, u in integration_health._OAUTH_CHECKS if k == "google_contacts")
        self.assertTrue(con.startswith(contacts._PEOPLE_LIST_URL))

    def test_no_probe_url_is_a_bare_identity_style_endpoint_the_real_tools_never_call(self):
        # the specific shape of the original bug: calendarList and bare people/me are real Google endpoints, just never ones her tools use
        banned = ("calendar/v3/users/me/calendarList", "people.googleapis.com/v1/people/me?", "drive/v3/about")
        for key, _provider, url in integration_health._OAUTH_CHECKS:
            for b in banned:
                self.assertNotIn(b, url, key)

    def test_the_sharepoint_probe_carries_the_mandatory_search_param(self):
        # /sites has no bare "list all" verb -- Graph requires $search or it 400s, which would be the SAME false-alarm bug in a new shape
        url = next(u for k, _p, u in integration_health._OAUTH_CHECKS if k == "sharepoint")
        self.assertIn("search=", url)

    def test_real_endpoint_urls_strips_placeholders_to_a_stable_prefix(self):
        urls = integration_health.real_endpoint_urls(drive)
        self.assertIn(drive._ONEDRIVE_ROOT_CHILDREN_URL, urls)
        self.assertNotIn("{", " ".join(urls))


if __name__ == "__main__":
    unittest.main()

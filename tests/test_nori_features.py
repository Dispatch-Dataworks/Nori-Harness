# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Optional features (features.py, 2026-10-29): trackers, inventory and meal planning can each be
switched off per person. Off must mean all of: the page and its menu/quick-bar/board links are gone
(and a direct visit is sent to Settings -> Features), its proactive pings never fire, its tools leave
Nori's tool list and refuse if called, and Nori can still flip the switch herself through the existing
update_settings tool. Real server, config, scheduler and tools; no network."""
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_features_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_features_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import config  # noqa: E402
import features  # noqa: E402
import household  # noqa: E402,F401 -- registers household_inventory and its tools
import meals  # noqa: E402,F401 -- registers meal_planning and its tools
import scheduler  # noqa: E402
import server  # noqa: E402
import settings_tool  # noqa: E402
import store  # noqa: E402
import tools  # noqa: E402
import trackers  # noqa: E402,F401 -- registers the tracker_* tools

store.init()
_admin = accounts.bootstrap_admin("Tester", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
TOKEN = accounts.new_session(_admin, "127.0.0.1")
CSRF = accounts.get_session(TOKEN)["csrf"]
SESS = accounts.get_session(TOKEN)
config.set("workspace", WS, "setup_complete", True)  # past first-run, so "/" is the chat

_httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
_httpd.daemon_threads = True
BASE = f"http://127.0.0.1:{_httpd.server_address[1]}"
threading.Thread(target=_httpd.serve_forever, daemon=True).start()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http(path, form=None, headers=None):
    data = None if form is None else urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST" if form is not None else "GET")
    req.add_header("Cookie", f"ns={TOKEN}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _opener.open(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers


def _all_on():
    for k in features.config_keys():
        config.set("user", UID, k, True)


def _off(key):
    config.set("user", UID, features.config_key(key), False)


class FeatureTable(unittest.TestCase):
    def setUp(self):
        _all_on()

    def test_the_three_features_exist_and_default_on(self):
        self.assertEqual({f["key"] for f in features.FEATURES}, {"trackers", "inventory", "meals"})
        for f in features.registry():
            default, typ, scope, secret, _label = config._SPEC[f["config_key"]]
            self.assertTrue(default, "on by default: nothing changes until someone switches one off")
            self.assertIs(typ, bool)
            self.assertEqual(scope, "user")
            self.assertFalse(secret)

    def test_every_switch_is_something_nori_can_change_with_update_settings(self):
        for k in features.config_keys():
            self.assertIn(k, settings_tool.PEER_SETTINGS_KEYS)
        schema = tools.schema_for("update_settings")["function"]["parameters"]["properties"]["field"]["enum"]
        for k in features.config_keys():
            self.assertIn(k, schema)

    def test_every_signal_that_names_a_feature_names_a_real_one_and_the_two_household_ones_do(self):
        keys = {f["key"] for f in features.FEATURES}
        by = {s["key"]: s["feature"] for s in scheduler.signal_registry()}
        for k, feature in by.items():
            self.assertTrue(feature is None or feature in keys, k)
        self.assertEqual(by["household_inventory"], "inventory")
        self.assertEqual(by["meal_planning"], "meals")
        with self.assertRaises(ValueError):
            scheduler.register_signal(lambda uid: None, key="zz_bad_feature", label="x", feature="nope")

    def test_every_tool_a_feature_claims_exists_and_no_two_features_claim_one(self):
        for f in features.FEATURES:
            for name in f["tools"]:
                self.assertIsNotNone(tools.schema_for(name), name)
            self.assertTrue(f["tools"] or f["tool_prefixes"])
        claimed = {}
        for name in tools._REGISTRY:
            owner = features.feature_for_tool(name)
            if owner:
                claimed[name] = owner
        self.assertEqual(claimed["tracker_log"], "trackers")
        self.assertEqual(claimed["plan_meal"], "meals")
        self.assertEqual(claimed["list_household_items"], "inventory")
        self.assertNotIn("update_settings", claimed)

    def test_paths(self):
        self.assertEqual(features.feature_for_path("/trackers"), "trackers")
        self.assertEqual(features.feature_for_path("/trackers/types/3/log"), "trackers")
        self.assertEqual(features.feature_for_path("/inventory/upsert"), "inventory")
        self.assertEqual(features.feature_for_path("/meals"), "meals")
        self.assertIsNone(features.feature_for_path("/mealsx"))
        self.assertIsNone(features.feature_for_path("/settings"))

    def test_an_unknown_feature_key_is_on(self):
        self.assertTrue(features.enabled(UID, "does-not-exist"))


class PingsStop(unittest.TestCase):
    def setUp(self):
        _all_on()
        for k in ("household_inventory", "meal_planning"):
            config.set("user", UID, f"ping_signal_{k}_enabled", True)

    def _fires(self, key):
        r = scheduler.outstanding_reason(UID)
        return bool(r) and r["key"] == key

    def test_the_meal_ping_fires_then_stops_when_meal_planning_is_off(self):
        saved = list(scheduler._SIGNAL_PROVIDERS)
        scheduler._SIGNAL_PROVIDERS[:] = [s for s in saved if s["key"] == "meal_planning"]
        try:
            self.assertTrue(self._fires("meal_planning"), "no dinner planned: the signal is true")
            _off("meals")
            self.assertIsNone(scheduler.outstanding_reason(UID))
            _all_on()
            self.assertTrue(self._fires("meal_planning"))
        finally:
            scheduler._SIGNAL_PROVIDERS[:] = saved

    def test_an_off_feature_never_calls_its_signal_function_at_all(self):
        called = []
        saved = list(scheduler._SIGNAL_PROVIDERS)
        scheduler._SIGNAL_PROVIDERS[:] = [dict(s, fn=lambda uid, called=called: called.append(uid)) for s in saved
                                          if s["key"] == "household_inventory"]
        try:
            _off("inventory")
            scheduler.outstanding_reason(UID)
            self.assertEqual(called, [])
            _all_on()
            scheduler.outstanding_reason(UID)
            self.assertEqual(called, [UID])
        finally:
            scheduler._SIGNAL_PROVIDERS[:] = saved

    def test_switching_inventory_off_leaves_the_meal_ping_alone(self):
        _off("inventory")
        saved = list(scheduler._SIGNAL_PROVIDERS)
        scheduler._SIGNAL_PROVIDERS[:] = [s for s in saved if s["key"] in ("household_inventory", "meal_planning")]
        try:
            r = scheduler.outstanding_reason(UID)
            self.assertEqual(r["key"], "meal_planning")
        finally:
            scheduler._SIGNAL_PROVIDERS[:] = saved


class ToolsLeave(unittest.TestCase):
    def setUp(self):
        _all_on()

    def _names(self):
        return {s["function"]["name"] for s in tools.active_schemas(SESS)}

    def test_each_features_tools_leave_the_list_and_come_back(self):
        self.assertIn("plan_meal", self._names())
        self.assertIn("tracker_log", self._names())
        self.assertIn("update_household_item", self._names())
        _off("meals")
        n = self._names()
        self.assertNotIn("plan_meal", n)
        self.assertNotIn("list_meal_plan", n)
        self.assertIn("tracker_log", n)
        self.assertIn("update_household_item", n)
        _off("trackers")
        _off("inventory")
        n = self._names()
        self.assertFalse([x for x in n if x.startswith("tracker_")])
        self.assertNotIn("update_household_item", n)
        self.assertIn("update_settings", n, "she must still be able to turn them back on")
        _all_on()
        self.assertIn("plan_meal", self._names())

    def test_calling_one_anyway_is_refused_and_says_how_to_turn_it_back_on(self):
        _off("meals")
        out = tools.dispatch("plan_meal", {"meal_date": "2026-11-01", "description": "soup"}, SESS)
        self.assertIn("error", out)
        self.assertIn("feature_meals_enabled", out["error"])
        self.assertEqual(meals.list_meals(SESS, "2026-11-01", "2026-11-01")["meals"], [], "nothing was saved")

    def test_nori_can_turn_a_feature_off_and_on_with_update_settings(self):
        out = tools.dispatch("update_settings", {"field": "feature_meals_enabled", "value": False}, SESS)
        self.assertTrue(out.get("ok"), out)
        self.assertFalse(features.enabled(UID, "meals"))
        self.assertNotIn("plan_meal", self._names())
        out = tools.dispatch("update_settings", {"field": "feature_meals_enabled", "value": True}, SESS)
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(features.enabled(UID, "meals"))
        got = tools.dispatch("get_settings", {}, SESS)["settings"]["feature_meals_enabled"]
        self.assertTrue(got["value"])
        self.assertTrue(got["editable"])

    def test_it_is_per_person(self):
        _id, raw = accounts.create_invite(WS, "Other", "member", UID)
        other = accounts.accept_invite(raw, "otherpass123")
        _off("meals")
        self.assertFalse(features.tool_allowed("plan_meal", UID))
        self.assertTrue(features.tool_allowed("plan_meal", other["id"]))
        self.assertTrue(features.enabled(other["id"], "meals"))


class Pages(unittest.TestCase):
    def setUp(self):
        _all_on()

    def test_links_are_there_by_default(self):
        status, page, _ = http("/")
        self.assertEqual(status, 200)
        for href in ("/inventory", "/meals", "/trackers"):
            self.assertIn(f"href='{href}'", page)

    def test_an_off_feature_loses_its_links_everywhere_and_the_others_keep_theirs(self):
        _off("meals")
        _off("trackers")
        for path in ("/", "/files"):
            status, page, _ = http(path)
            self.assertEqual(status, 200, path)
            self.assertNotIn("href='/meals'", page, path)
            self.assertNotIn("href='/trackers'", page, path)
            self.assertIn("href='/inventory'", page, path)
        status, page, _ = http("/")
        self.assertNotIn("class=board-trackers-btn", page)
        _all_on()
        status, page, _ = http("/")
        self.assertIn("class=board-trackers-btn", page)

    def test_a_visit_to_an_off_page_goes_to_settings_features(self):
        _off("meals")
        for path in ("/meals", "/meals?start=2026-11-01"):
            status, _body, headers = http(path)
            self.assertEqual(status, 303, path)
            self.assertTrue(headers["Location"].startswith("/settings?tab=features"), headers["Location"])
        status, body, _ = http(headers["Location"])
        self.assertEqual(status, 200)
        self.assertIn("Meal planning is switched off", body)

    def test_a_post_to_an_off_feature_saves_nothing(self):
        _off("meals")
        status, _b, headers = http("/meals/set", {"csrf": CSRF, "meal_date": "2026-11-02", "description": "stew"})
        self.assertEqual(status, 303)
        self.assertIn("tab=features", headers["Location"])
        self.assertEqual(meals.list_meals(SESS, "2026-11-02", "2026-11-02")["meals"], [])
        _off("inventory")
        status, _b, _h = http("/inventory/upsert", {"csrf": CSRF, "name": "rice", "status": "low"})
        self.assertEqual(status, 303)
        self.assertEqual(household.list_items(SESS)["items"], [])

    def test_the_data_survives_being_switched_off_and_on(self):
        meals.set_meal(SESS, "2026-11-03", "curry")
        _off("meals")
        _all_on()
        self.assertEqual([m["description"] for m in meals.list_meals(SESS, "2026-11-03", "2026-11-03")["meals"]], ["curry"])

    def test_the_features_tab_lists_every_feature_and_saves_the_switches(self):
        status, page, _ = http("/settings?tab=features")
        self.assertEqual(status, 200)
        for f in features.registry():
            self.assertIn(f"name='{f['config_key']}' checked", page)
        status, _b, headers = http("/settings/features", {"csrf": CSRF, "feature_inventory_enabled": "on"})
        self.assertIn(status, (200, 303))
        self.assertTrue(features.enabled(UID, "inventory"))
        self.assertFalse(features.enabled(UID, "meals"))
        self.assertFalse(features.enabled(UID, "trackers"))
        status, page, _ = http("/settings?tab=features")
        self.assertIn("name='feature_inventory_enabled' checked", page)
        self.assertNotIn("name='feature_meals_enabled' checked", page)

    def test_the_android_app_gets_a_link_to_its_own_settings_in_the_menu_and_a_browser_does_not(self):
        for path in ("/", "/files", "/settings"):
            self.assertNotIn("nori-app://settings", http(path)[1], path)
            self.assertIn("<a href='nori-app://settings'>App settings</a>",
                          http(path, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/120 Mobile Safari/537.36 NoriApp/0.2.0"})[1], path)

    def test_the_pings_tab_says_why_a_signal_is_quiet(self):
        _off("meals")
        status, page, _ = http("/settings?tab=pings")
        self.assertEqual(status, 200)
        self.assertIn("off while Meal planning is switched off", page)
        self.assertNotIn("off while Inventory is switched off", page)


if __name__ == "__main__":
    unittest.main()

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Context tuning as three layers (nori/tuning_admin.py): shipped defaults, a deliberately-set dated baseline, and the live values. The baseline must survive everything else."""
import ast
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
N = ROOT
_P = Path(tempfile.mkdtemp(prefix="nori_tuning_prompts_"))
os.environ["NORI_PROMPTS_DIR"] = str(_P)
os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_tuning_data_")
os.environ["NORI_NO_LOGFILE"] = "1"
sys.path.insert(0, str(N))

import config  # noqa: E402
import persona  # noqa: E402
import persona_admin  # noqa: E402
import promptdoc  # noqa: E402
import store  # noqa: E402
import tuning_admin as T  # noqa: E402

store.init()
WS = 1


def reset_state():
    for k in T.KEYS:
        store.write(lambda c, k=k: c.execute("DELETE FROM settings WHERE scope='workspace' AND scope_id=? AND key=?", (WS, k)))
    store.write(lambda c: c.execute("DROP TRIGGER IF EXISTS trg_tuning_baseline_nodelete"))
    store.write(lambda c: c.execute("DELETE FROM tuning_baseline"))
    store.write(lambda c: c.execute("DROP TRIGGER IF EXISTS trg_tuning_history_noupdate"))
    store.write(lambda c: c.execute("DELETE FROM tuning_history"))
    store.init()                                                                   # recreates the triggers


def good(body):
    return f"{promptdoc.START}\n{body}\n{promptdoc.END}\n"


class Base(unittest.TestCase):
    def setUp(self):
        reset_state()
        for p in _P.glob("*"):
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        shutil.copy(N / "prompts" / "persona.default.md", _P / "persona.default.md")


class FreshInstallTests(Base):
    def test_a_fresh_install_runs_on_the_shipped_defaults_with_no_baseline(self):
        self.assertEqual(T.current(WS), T.defaults())
        self.assertIsNone(T.baseline(WS))
        self.assertIsNone(T.previous(WS))
        html = T.render_layers(WS, "c")
        self.assertIn("not saved one yet", html.replace("have not", "not"))
        self.assertNotIn("target=baseline", html)
        self.assertNotIn("target=previous", html)

    def test_the_shipped_defaults_are_the_configs_own_and_never_change(self):
        d = T.defaults()
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 77}})
        T.act(WS, 1, "baseline_save", {})
        self.assertEqual(T.defaults(), d)
        self.assertEqual(d["context_window_msgs"], config.spec()["context_window_msgs"][0])


class SaveTests(Base):
    def test_a_save_changes_values_and_records_what_it_replaced(self):
        ok, msg = T.act(WS, 1, "save", {"values": {"context_window_msgs": 45, "memory_max_tokens": 500}})
        self.assertTrue(ok, msg)
        self.assertEqual(T.current(WS)["context_window_msgs"], 45)
        h = T.history(WS)
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["values"]["context_window_msgs"], T.defaults()["context_window_msgs"])

    def test_a_bad_value_changes_nothing_at_all_including_the_good_ones_beside_it(self):
        before = T.current(WS)
        for bad in ({"context_window_msgs": 45, "memory_max_tokens": 999999}, {"context_window_msgs": "abc"}, {"peer_recent_cap": 0}):
            ok, msg = T.act(WS, 1, "save", {"values": bad})
            self.assertFalse(ok, bad)
            self.assertEqual(T.current(WS), before, bad)
        self.assertEqual(T.history(WS), [])

    def test_an_unchanged_save_writes_no_history(self):
        T.act(WS, 1, "save", {"values": {"context_window_msgs": T.defaults()["context_window_msgs"]}})
        self.assertEqual(T.history(WS), [])

    def test_unknown_keys_are_ignored_not_written(self):
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 40, "ping_enabled": 0, "web_search_enabled": 0}})
        self.assertEqual(config.get("workspace", WS, "web_search_enabled"), config.spec()["web_search_enabled"][0])

    def test_an_unknown_action_is_refused(self):
        self.assertEqual(T.act(WS, 1, "wipe", {}), (False, "unknown action"))


class BaselineTests(Base):
    def test_the_baseline_is_set_only_by_the_deliberate_action(self):
        for v in (40, 50, 60):
            T.act(WS, 1, "save", {"values": {"context_window_msgs": v}})
        T.act(WS, 1, "reset_default", {})
        T.act(WS, 1, "previous", {})
        self.assertIsNone(T.baseline(WS), "edits, resets and restores must never create or move the baseline")

    def test_it_records_when_it_was_saved_and_who_and_shows_the_age(self):
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 40}})
        t0 = time.time()
        ok, _ = T.act(WS, 7, "baseline_save", {})
        self.assertTrue(ok)
        b = T.baseline(WS)
        self.assertEqual(b["values"]["context_window_msgs"], 40)
        self.assertGreaterEqual(b["saved_ts"], t0 - 1)
        self.assertEqual(store.read(lambda c: c.execute("SELECT saved_by FROM tuning_baseline WHERE workspace_id=?", (WS,)).fetchone()["saved_by"]), 7)
        html = T.render_layers(WS, "c")
        self.assertIn("Saved 20", html)
        self.assertIn("just now", html)

    def test_the_baseline_survives_every_other_action_and_a_migration_and_settings_churn(self):
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 40, "memory_max_tokens": 600}})
        T.act(WS, 1, "baseline_save", {})
        saved = T.baseline(WS)
        for v in range(41, 70):
            T.act(WS, 1, "save", {"values": {"context_window_msgs": v}})
        T.act(WS, 1, "reset_default", {})
        T.act(WS, 1, "previous", {})
        T.act(WS, 1, "restore", {"id": T.history(WS)[-1]["id"]})
        T.act(WS, 1, "save", {"values": {"memory_max_tokens": 999999}})                # refused
        for k in T.KEYS:                                                               # raw settings churn: the settings table is not where the baseline lives
            config.set("workspace", WS, k, config.spec()[k][0])
        for k in T.KEYS:
            store.write(lambda c, k=k: c.execute("DELETE FROM settings WHERE key=?", (k,)))
        store.init()                                                                   # a migration/startup pass, twice
        store.init()
        self.assertEqual(T.baseline(WS), saved)

    def test_a_baseline_restore_of_the_persona_cannot_touch_it(self):
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 40}})
        T.act(WS, 1, "baseline_save", {})
        saved = T.baseline(WS)
        persona_admin.act("save", {"text": good("p1")})
        persona_admin.act("baseline_save", {})
        persona_admin.act("save", {"text": good("p2")})
        persona_admin.act("baseline_revert", {})
        persona_admin.act("reset_default", {})
        self.assertEqual(T.baseline(WS), saved)

    def test_the_database_itself_refuses_to_delete_a_baseline(self):
        T.act(WS, 1, "baseline_save", {})
        with self.assertRaises(sqlite3.DatabaseError):
            store.write(lambda c: c.execute("DELETE FROM tuning_baseline WHERE workspace_id=?", (WS,)))
        with self.assertRaises(sqlite3.DatabaseError):
            store.write(lambda c: c.execute("DELETE FROM tuning_baseline"))
        self.assertIsNotNone(T.baseline(WS))

    def test_the_schema_text_keeps_the_delete_guard(self):
        self.assertIn("trg_tuning_baseline_nodelete", store.SCHEMA)
        self.assertNotRegex(store.SCHEMA, r"(?i)drop\s+table[^;]*tuning_baseline")

    def test_nothing_but_the_tuning_module_writes_the_baseline_table(self):
        writers = []
        for p in N.glob("*.py"):
            src = p.read_text(encoding="utf-8")
            if re.search(r"(INSERT\s+INTO|UPDATE|DELETE\s+FROM|DROP\s+TABLE)\s+tuning_baseline", src, re.I) and p.name not in ("store.py", "tuning_admin.py"):
                writers.append(p.name)
        self.assertEqual(writers, [])
        self.assertEqual(len(re.findall(r"DELETE\s+FROM\s+tuning_baseline", (N / "tuning_admin.py").read_text(encoding="utf-8"), re.I)), 0)

    def test_baselines_are_per_workspace(self):
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 40}})
        T.act(WS, 1, "baseline_save", {})
        self.assertIsNone(T.baseline(2))

    def test_saving_a_new_baseline_is_the_only_thing_that_replaces_it(self):
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 40}})
        T.act(WS, 1, "baseline_save", {})
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 50}})
        T.act(WS, 1, "baseline_save", {})
        self.assertEqual(T.baseline(WS)["values"]["context_window_msgs"], 50)


class RestoreTargetTests(Base):
    def setUp(self):
        super().setUp()
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 40, "memory_max_tokens": 500}})
        T.act(WS, 1, "baseline_save", {})                                              # baseline: 40 / 500
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 60}})
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 90, "memory_max_tokens": 800}})   # live: 90 / 800

    def test_back_to_baseline(self):
        ok, msg = T.act(WS, 1, "baseline_revert", {})
        self.assertTrue(ok, msg)
        self.assertEqual((T.current(WS)["context_window_msgs"], T.current(WS)["memory_max_tokens"]), (40, 500))

    def test_back_to_the_shipped_defaults(self):
        T.act(WS, 1, "reset_default", {})
        self.assertEqual(T.current(WS), T.defaults())

    def test_back_one_edit(self):
        T.act(WS, 1, "previous", {})
        self.assertEqual(T.current(WS)["context_window_msgs"], 60)
        self.assertEqual(T.current(WS)["memory_max_tokens"], 500)

    def test_back_one_edit_skips_an_earlier_state_that_equals_the_live_values(self):
        reset_state()
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 40}})                  # history: [default]
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 50}})                  # history: [40, default]
        config.set("workspace", WS, "context_window_msgs", 40)                         # live changed outside these actions: now equals the newest history entry
        self.assertEqual(T.previous(WS)["values"]["context_window_msgs"], T.defaults()["context_window_msgs"])

    def test_a_named_earlier_version(self):
        first = T.history(WS)[-1]
        T.act(WS, 1, "restore", {"id": first["id"]})
        self.assertEqual(T.current(WS), first["values"])

    def test_every_restore_keeps_what_it_replaced_so_it_can_be_undone(self):
        live = T.current(WS)
        T.act(WS, 1, "reset_default", {})
        self.assertIn(live, [h["values"] for h in T.history(WS)])
        T.act(WS, 1, "previous", {})
        self.assertEqual(T.current(WS), live)

    def test_restoring_something_that_is_not_there_changes_nothing(self):
        reset_state()
        live = T.current(WS)
        for action, form in (("baseline_revert", {}), ("previous", {}), ("restore", {"id": 99999}), ("restore", {"id": "x"}), ("restore", {})):
            ok, _ = T.act(WS, 1, action, form)
            self.assertFalse(ok, action)
        self.assertEqual(T.current(WS), live)

    def test_a_baseline_that_no_longer_fits_the_bounds_is_refused_whole(self):
        store.write(lambda c: c.execute("UPDATE tuning_baseline SET values_json=? WHERE workspace_id=?",
                                        ('{"context_window_msgs": 5000, "compaction_enabled": true, "compaction_max_segments": 8, "compaction_budget_tokens": 500, '
                                         '"compaction_session_gap_hours": 2.0, "peer_recent_cap": 5, "peer_recent_window_hours": 12, "memory_max_tokens": 375, "memory_pinned_max_tokens": 150}', WS)))
        live = T.current(WS)
        ok, msg = T.act(WS, 1, "baseline_revert", {})
        self.assertFalse(ok)
        self.assertEqual(T.current(WS), live)

    def test_a_preview_lists_only_what_would_change_and_writes_nothing(self):
        live, hist = T.current(WS), T.history(WS)
        out = T.render_preview(WS, "baseline", None, "tok")
        self.assertIn("context_window_msgs", out)
        self.assertIn("memory_max_tokens", out)
        self.assertNotIn("peer_recent_cap", out)                                       # unchanged values are not listed
        self.assertEqual(T.current(WS), live)
        self.assertEqual(T.history(WS), hist)

    def test_a_preview_of_the_live_values_says_nothing_would_change(self):
        T.act(WS, 1, "reset_default", {})
        out = T.render_preview(WS, "default", None, "tok")
        self.assertIn("would change nothing", out)

    def test_previews_of_missing_targets_are_none(self):
        reset_state()
        self.assertIsNone(T.render_preview(WS, "baseline", None, "t"))
        self.assertIsNone(T.render_preview(WS, "previous", None, "t"))
        self.assertIsNone(T.render_preview(WS, "history", "nope", "t"))
        self.assertIsNone(T.render_preview(WS, "zzz", None, "t"))

    def test_the_three_targets_post_to_three_different_actions(self):
        acts = {t: re.search(r"action='/admin/contexttuning/(\w+)'", T.render_preview(WS, t, None, "c")).group(1) for t in ("baseline", "default", "previous")}
        self.assertEqual(acts, {"baseline": "baseline_revert", "default": "reset_default", "previous": "previous"})

    def test_history_is_append_only(self):
        with self.assertRaises(sqlite3.DatabaseError):
            store.write(lambda c: c.execute("UPDATE tuning_history SET values_json='{}'"))


class IndependenceTests(Base):
    """A bad persona edit must not cost good tuning, and the reverse."""

    def test_tuning_restores_never_touch_the_persona(self):
        persona_admin.act("save", {"text": good("my persona")})
        persona_admin.act("baseline_save", {})
        pfile = persona.PERSONA_PATH.read_text(encoding="utf-8")
        pbase = persona.baseline_text()
        hist = persona.history()
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 40}})
        T.act(WS, 1, "baseline_save", {})
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 90}})
        for a in ("baseline_revert", "reset_default", "previous"):
            T.act(WS, 1, a, {})
        self.assertEqual(persona.PERSONA_PATH.read_text(encoding="utf-8"), pfile)
        self.assertEqual(persona.baseline_text(), pbase)
        self.assertEqual(persona.history(), hist)

    def test_persona_restores_never_touch_the_tuning(self):
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 40, "memory_max_tokens": 777}})
        T.act(WS, 1, "baseline_save", {})
        T.act(WS, 1, "save", {"values": {"context_window_msgs": 55}})
        live, base, hist = T.current(WS), T.baseline(WS), T.history(WS)
        persona_admin.act("save", {"text": good("a")})
        persona_admin.act("baseline_save", {})
        persona_admin.act("save", {"text": good("b")})
        for a in ("baseline_revert", "reset_default"):
            persona_admin.act(a, {})
        persona_admin.act("restore", {"name": persona.history()[0]["name"]})
        self.assertEqual(T.current(WS), live)
        self.assertEqual(T.baseline(WS), base)
        self.assertEqual(T.history(WS), hist)


class OperatorOnlyTests(unittest.TestCase):
    def test_the_server_form_and_the_tuning_module_agree_on_which_values_are_tuned(self):
        tree = ast.parse((N / "server.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
        fields = next(a for a in cls.body if isinstance(a, ast.Assign) and getattr(a.targets[0], "id", "") == "_CONTEXT_FIELDS")
        keys = tuple(e.elts[0].value for e in fields.value.elts)
        self.assertEqual(keys, T.KEYS)

    def test_every_server_method_that_uses_the_tuning_module_checks_for_an_admin(self):
        tree = ast.parse((N / "server.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
        users = [f for f in cls.body if isinstance(f, ast.FunctionDef) and "tuning_admin" in ast.unparse(f) and f.name not in ("_do_GET_inner", "do_GET", "do_POST", "_do_POST_inner")]
        self.assertGreaterEqual(len(users), 4)
        for f in users:
            self.assertIn("sess['role'] != 'admin'", ast.unparse(f), f"{f.name} reaches the tuning module without an admin check")

    def test_the_post_routes_sit_behind_the_global_csrf_check(self):
        src = (N / "server.py").read_text(encoding="utf-8")
        self.assertLess(src.index("if not self.csrf_ok(sess, form):"), src.index('if path.startswith("/admin/contexttuning/") and path['))

    def test_a_member_is_refused_on_every_tuning_route(self):
        from test_nori_navigation import handler
        for call, args in (("context_preview_get", lambda s: (s, {"target": ["baseline"]})), ("context_admin_action", lambda s: (s, {}, "reset_default")),
                           ("context_admin_post", lambda s: (s, {"context_window_msgs": "5"}))):
            h, sess, env = handler("member")
            env["tuning_admin"] = Mock()
            env["config"] = Mock()
            getattr(h, call)(*args(sess))
            self.assertEqual(h.send.call_args.args[0], 403, call)
            self.assertEqual(env["tuning_admin"].mock_calls, [], call)

    def test_no_tool_can_reach_the_tuning_module_or_its_tables(self):
        for p in N.glob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            registers = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("register", "register_peer_actions")
                            and isinstance(n.func.value, ast.Name) and n.func.value.id == "tools" for n in ast.walk(tree))
            mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
            if registers:
                self.assertFalse(mods & {"tuning_admin", "restore_ui", "persona_admin"}, p.name)
                self.assertNotRegex(p.read_text(encoding="utf-8"), r"tuning_(baseline|history)", p.name)

    def test_no_settings_tool_key_is_a_tuning_key_or_a_baseline(self):
        import settings_tool
        self.assertEqual(set(T.KEYS) & set(settings_tool.PEER_SETTINGS_KEYS), set())
        for k in config.readable_spec():
            self.assertNotRegex(k, r"baseline")


if __name__ == "__main__":
    unittest.main()

# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The persona editor (nori/persona_admin.py): operator-only, never reachable by a tool, always recoverable, and correct on a fresh install."""
import ast
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
N = ROOT
_P = Path(tempfile.mkdtemp(prefix="nori_persona_prompts_"))
os.environ["NORI_PROMPTS_DIR"] = str(_P)                     # before promptdoc is imported: it reads this once
os.environ.setdefault("NORI_DATA_DIR", tempfile.mkdtemp(prefix="nori_persona_data_"))
os.environ.setdefault("NORI_NO_LOGFILE", "1")
sys.path.insert(0, str(N))

import persona  # noqa: E402
import persona_admin  # noqa: E402
import promptdoc  # noqa: E402

SHIPPED = (N / "prompts" / "persona.default.md").read_text(encoding="utf-8")


def good(body="You are a test persona. Be brief."):
    return f"{promptdoc.START}\n{body}\n{promptdoc.END}\n"


def _default_rendered() -> str:
    """default_text() is deliberately never substituted (it's what a reset
    WRITES to disk -- see persona.load_prompt()'s own docstring on why
    baking a name into the file would be the exact thing 2026-09-25's
    assistant-name feature exists to avoid). load_prompt() with no user_id
    (every test here, same as an admin preview/reset path) substitutes the
    shipped default name, "Nori" -- this mirrors that one substitution so
    the two can still be compared."""
    return persona.default_text().split(promptdoc.START, 1)[1].split(promptdoc.END, 1)[0].strip() \
        .replace("{{ASSISTANT_NAME}}", "Nori")


class Fresh(unittest.TestCase):
    def setUp(self):
        for p in _P.glob("*"):
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        shutil.copy(N / "prompts" / "persona.default.md", _P / "persona.default.md")


class FreshInstallTests(Fresh):
    def test_with_no_persona_file_the_shipped_default_is_what_she_gets(self):
        self.assertFalse(persona.PERSONA_PATH.exists())
        self.assertEqual(persona.load_prompt(), _default_rendered())
        self.assertGreater(len(persona.load_prompt()), 500)
        self.assertFalse(persona_admin.status()["customized"])
        self.assertIn("shipped default", persona_admin.render("c"))

    def test_the_shipped_default_is_a_valid_persona_doc(self):
        self.assertIsNone(promptdoc.PromptDoc("persona").validate(SHIPPED))
        # The check that the shipped default carries no operator-specific
        # identifiers is maintained separately, outside this repository --
        # a public test naming the private words it checks for is the
        # same leak that check itself exists to prevent.

    def test_the_shipped_default_assumes_nothing_about_the_users_gender(self):
        self.assertIsNone(re.search(r"\b(he|him|his|she|her|hers)\b", SHIPPED, re.I), "the shipped default must use neutral wording for the person she works for")

    def test_first_edit_creates_the_file_and_the_default_stays_recoverable(self):
        ok, _ = persona_admin.act("save", {"text": good()})
        self.assertTrue(ok)
        self.assertTrue(persona.PERSONA_PATH.exists())
        self.assertEqual(persona.load_prompt(), "You are a test persona. Be brief.")
        ok, _ = persona_admin.act("reset_default", {})
        self.assertTrue(ok)
        self.assertEqual(persona.load_prompt(), _default_rendered())


class SaveAndRecoverTests(Fresh):
    def test_every_save_keeps_the_text_it_replaced(self):
        persona_admin.act("save", {"text": good("one")})
        persona_admin.act("save", {"text": good("two")})
        persona_admin.act("save", {"text": good("three")})
        h = persona.history()
        self.assertEqual(len(h), 2)
        self.assertEqual(sorted(persona.history_text(x["name"]).split("\n")[1] for x in h), ["one", "two"])
        self.assertEqual(persona.load_prompt(), "three")

    def test_a_rejected_save_changes_nothing_and_says_why(self):
        persona_admin.act("save", {"text": good("kept")})
        before = persona.PERSONA_PATH.read_text(encoding="utf-8")
        n = len(persona.history())
        for bad, why in (("", "empty"), ("just some text", "markers"), (f"{promptdoc.END}\nx\n{promptdoc.START}", "order"), (f"{promptdoc.START}\n   \n{promptdoc.END}", "nothing between"),
                         (good("x" * (promptdoc.MAX_BYTES + 10)), "too large")):
            ok, msg = persona_admin.act("save", {"text": bad})
            self.assertFalse(ok, why)
            self.assertIn(why.split()[0], msg)
        ok, _ = persona_admin.act("save", {"text": None})
        self.assertFalse(ok)
        self.assertEqual(persona.PERSONA_PATH.read_text(encoding="utf-8"), before)
        self.assertEqual(len(persona.history()), n)

    def test_a_rejected_save_keeps_what_the_operator_typed_in_the_editor(self):
        html = persona_admin.render("c", err="not saved: markers", draft="my long carefully written draft")
        self.assertIn("my long carefully written draft", html)
        self.assertIn("not saved: markers", html)

    def test_a_live_file_emptied_on_disk_never_sends_her_an_empty_prompt(self):
        persona.PERSONA_PATH.write_text(good(""), encoding="utf-8")            # edited by hand, bypassing validation
        self.assertGreater(len(persona.load_prompt()), 500)                    # falls back to the shipped default

    def test_restore_brings_back_an_earlier_version_and_keeps_the_replaced_one(self):
        persona_admin.act("save", {"text": good("first")})
        persona_admin.act("save", {"text": good("second")})
        name = persona.history()[0]["name"]
        ok, _ = persona_admin.act("restore", {"name": name})
        self.assertTrue(ok)
        self.assertEqual(persona.load_prompt(), "first")
        self.assertTrue(any("second" in persona.history_text(h["name"]) for h in persona.history()))

    def test_restore_refuses_a_name_that_is_not_a_history_file(self):
        for bad in ("../persona.default.md", "..\\x", "persona.md", "persona-1.md", "", None, 5, "persona-20260101-000000.md/../../x"):
            ok, _ = persona_admin.act("restore", {"name": bad})
            self.assertFalse(ok, repr(bad))
            self.assertIsNone(persona_admin.history_text(bad))

    def test_baseline_saves_and_reverts_and_survives_a_reset(self):
        persona_admin.act("save", {"text": good("my good one")})
        self.assertTrue(persona_admin.act("baseline_save", {})[0])
        persona_admin.act("save", {"text": good("an experiment that went badly")})
        self.assertTrue(persona_admin.act("baseline_revert", {})[0])
        self.assertEqual(persona.load_prompt(), "my good one")
        persona_admin.act("reset_default", {})
        self.assertTrue(persona.has_baseline())                                # a reset never touches the baseline
        self.assertTrue(persona_admin.act("baseline_revert", {})[0])
        self.assertEqual(persona.load_prompt(), "my good one")

    def test_reverting_with_no_baseline_is_a_refusal_not_a_crash(self):
        ok, msg = persona_admin.act("baseline_revert", {})
        self.assertFalse(ok)
        self.assertIn("baseline", msg)

    def test_an_unknown_action_is_refused(self):
        self.assertEqual(persona_admin.act("delete_everything", {}), (False, "unknown action"))

    def test_the_default_is_never_modified_by_any_action(self):
        before = (_P / "persona.default.md").read_text(encoding="utf-8")
        for a, f in (("save", {"text": good()}), ("baseline_save", {}), ("reset_default", {}), ("save", {"text": good("z")})):
            persona_admin.act(a, f)
        self.assertEqual((_P / "persona.default.md").read_text(encoding="utf-8"), before)

    def test_the_editor_text_is_escaped(self):
        persona_admin.act("save", {"text": good("</textarea><script>alert(1)</script>")})
        out = persona_admin.render("c")
        self.assertNotIn("<script>alert(1)</script>", out)
        self.assertIn("&lt;/textarea&gt;", out)


class BaselineLayerTests(Fresh):
    """Three layers: shipped default, a deliberately-set baseline with a date, and the live text."""

    def test_the_baseline_is_set_only_by_the_deliberate_action(self):
        self.assertFalse(persona.has_baseline())
        for t in ("one", "two", "three"):
            persona_admin.act("save", {"text": good(t)})
        persona_admin.act("reset_default", {})
        persona_admin.act("restore", {"name": persona.history()[0]["name"]})
        self.assertFalse(persona.has_baseline(), "an edit, reset or restore must never create or move the baseline")

    def test_it_records_when_it_was_saved_and_says_how_old_it_is(self):
        persona_admin.act("save", {"text": good("known good")})
        before = __import__("time").time()
        persona_admin.act("baseline_save", {})
        ts = persona.baseline_saved_ts()
        self.assertGreaterEqual(ts, before - 1)
        self.assertLessEqual(ts, __import__("time").time() + 1)
        self.assertTrue(persona.promptdoc.PromptDoc("persona").baseline_meta_path.is_file())
        os.utime(persona.promptdoc.PromptDoc("persona").baseline_path, (1_000_000_000, 1_000_000_000))       # a copied/restored file's mtime says nothing about when it was marked
        self.assertAlmostEqual(persona.baseline_saved_ts(), ts, delta=1)
        html = persona_admin.render("c")
        self.assertIn("Saved 20", html)
        self.assertIn("just now", html)

    def test_the_baseline_survives_every_other_action_and_history_pruning(self):
        persona_admin.act("save", {"text": good("my good one")})
        persona_admin.act("baseline_save", {})
        ts = persona.baseline_saved_ts()
        for i in range(promptdoc._KEEP_HISTORY + 15):                                   # far more saves than history keeps
            persona_admin.act("save", {"text": good(f"edit {i}")})
        persona_admin.act("reset_default", {})
        persona_admin.act("restore", {"name": persona.history()[-1]["name"]})
        persona_admin.act("save", {"text": "rejected"})
        self.assertEqual(persona.baseline_text().split(chr(10))[1], "my good one")
        self.assertEqual(persona.baseline_saved_ts(), ts)

    def test_saving_a_new_baseline_is_the_only_thing_that_replaces_it(self):
        persona_admin.act("save", {"text": good("first good")})
        persona_admin.act("baseline_save", {})
        persona_admin.act("save", {"text": good("second good")})
        persona_admin.act("baseline_save", {})
        self.assertEqual(persona.baseline_text().split(chr(10))[1], "second good")

    def test_each_target_previews_the_exact_change_before_anything_happens(self):
        persona_admin.act("save", {"text": good("baseline text")})
        persona_admin.act("baseline_save", {})
        persona_admin.act("save", {"text": good("live text")})
        persona_admin.act("save", {"text": good("newest live text")})
        live = persona.PERSONA_PATH.read_text(encoding="utf-8")
        hist = persona.history()
        for target, name, must_show, label in (("baseline", None, "baseline text", "your saved baseline"), ("default", None, "PROMPT STARTS", "the shipped default"),
                                                ("previous", None, "live text", "the version before your last edit"), ("history", hist[-1]["name"], "", "the version from")):
            out = persona_admin.render_preview(target, name, "tok")
            self.assertIsNotNone(out, target)
            self.assertIn(label, out)
            self.assertIn(must_show, out)
            self.assertIn("newest live text", out)                                   # the live line is shown as what would go
            self.assertIn("background:rgba(248,81,73", out)                          # removed
            self.assertIn("background:rgba(46,160,67", out)                          # added
        self.assertEqual(persona.PERSONA_PATH.read_text(encoding="utf-8"), live, "a preview must change nothing")
        self.assertEqual(persona.history(), hist)

    def test_a_preview_of_something_that_does_not_exist_is_none(self):
        self.assertIsNone(persona_admin.render_preview("baseline", None, "t"))                 # none saved
        self.assertIsNone(persona_admin.render_preview("previous", None, "t"))                 # no history
        self.assertIsNone(persona_admin.render_preview("history", "../../secret.key", "t"))
        self.assertIsNone(persona_admin.render_preview("nonsense", None, "t"))

    def test_the_three_targets_are_distinct_in_the_page_and_the_restore_buttons_go_to_different_actions(self):
        persona_admin.act("save", {"text": good("a")})
        persona_admin.act("save", {"text": good("b")})
        persona_admin.act("baseline_save", {})
        html = persona_admin.render("c")
        for title in ("Back to my baseline", "Back to the shipped default", "Back one edit"):
            self.assertIn(title, html)
        actions = {t: re.search(r"action='/admin/persona/(\w+)'", persona_admin.render_preview(t, None, "c")).group(1) for t in ("baseline", "default", "previous")}
        self.assertEqual(actions, {"baseline": "baseline_revert", "default": "reset_default", "previous": "restore"})

    def test_without_a_baseline_the_page_says_so_and_offers_no_baseline_restore(self):
        html = persona_admin.render("c")
        self.assertIn("not saved one yet", html.replace("have not", "not"))
        self.assertNotIn("target=baseline", html)

    def test_previous_restores_what_the_last_save_replaced(self):
        persona_admin.act("save", {"text": good("before")})
        persona_admin.act("save", {"text": good("after")})
        name = re.search(r"name='name' value='([^']+)'", persona_admin.render_preview("previous", None, "c")).group(1)
        persona_admin.act("restore", {"name": name})
        self.assertEqual(persona.load_prompt(), "before")

    def test_previous_is_the_most_recent_replaced_version_not_the_oldest(self):
        for t in ("v1", "v2", "v3", "v4"):
            persona_admin.act("save", {"text": good(t)})                        # history now holds v1, v2, v3
        persona_admin.act("restore", {"name": re.search(r"name='name' value='([^']+)'", persona_admin.render_preview("previous", None, "c")).group(1)})
        self.assertEqual(persona.load_prompt(), "v3")

    def test_a_preview_identical_to_the_live_text_says_nothing_would_change(self):
        persona_admin.act("reset_default", {})
        self.assertIn("would change nothing", persona_admin.render_preview("default", None, "c"))


class OperatorOnlyTests(unittest.TestCase):
    """Reachable from admin routes only; never from a tool."""

    def modules(self):
        return {p.name: ast.parse(p.read_text(encoding="utf-8")) for p in N.glob("*.py")}

    @staticmethod
    def imports(tree):
        out = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                out |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                out.add(n.module.split(".")[0])
        return out

    def test_no_module_that_registers_a_tool_can_reach_the_persona_code(self):
        family = {"persona", "persona_admin", "promptdoc", "guidance", "tuning_admin", "restore_ui"}
        offenders = []
        for name, tree in self.modules().items():
            registers = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("register", "register_peer_actions")
                            and isinstance(n.func.value, ast.Name) and n.func.value.id == "tools" for n in ast.walk(tree))
            if registers and self.imports(tree) & family:
                offenders.append(name)
        self.assertEqual(offenders, [], "a module that registers a model-callable tool must not import the persona/prompt editing code")

    def test_only_the_expected_modules_import_the_persona_code_at_all(self):
        allowed = {"persona": {"context.py", "persona_admin.py"}, "persona_admin": {"server.py"}, "promptdoc": {"persona.py", "guidance.py"}, "guidance": {"context.py"},
                   "tuning_admin": {"server.py"}, "restore_ui": {"persona_admin.py", "tuning_admin.py"}}
        for mod, ok_importers in allowed.items():
            importers = {name for name, tree in self.modules().items() if mod in self.imports(tree)}
            self.assertLessEqual(importers, ok_importers, f"{mod} is imported by {sorted(importers - ok_importers)}: a new importer widens who can reach prompt editing")

    def test_context_only_reads_the_persona_never_writes_it(self):
        src = (N / "context.py").read_text(encoding="utf-8")
        self.assertRegex(src, r"persona\.load_prompt\(")
        self.assertNotRegex(src, r"persona\.(save|restore|reset|revert)\w*\(")

    def test_no_setting_the_settings_tool_can_write_or_read_names_a_prompt(self):
        import config
        import settings_tool
        for k in settings_tool.PEER_SETTINGS_KEYS:
            self.assertNotRegex(k, r"persona|prompt|guidance")
        for k in config.readable_spec():
            self.assertNotRegex(k, r"persona|prompt|guidance", f"{k}: a readable setting must not be a way to reach the persona")

    def test_no_tool_name_or_schema_offers_persona_editing(self):
        import tools
        try:
            import server  # noqa: F401  -- registers every native tool
        except Exception:
            pass
        for name, t in tools._REGISTRY.items():
            self.assertNotRegex(name, r"persona", name)

    def test_every_server_method_that_uses_the_editor_checks_for_an_admin(self):
        tree = ast.parse((N / "server.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
        users = [f for f in cls.body if isinstance(f, ast.FunctionDef) and "persona_admin" in ast.unparse(f) and f.name not in ("_settings_groups",)]
        self.assertTrue(users, "the persona routes are gone or renamed")
        for f in users:
            if f.name in ("do_GET", "_do_GET_inner", "do_POST", "_do_POST_inner"):
                continue
            src = ast.unparse(f)
            self.assertIn("sess['role'] != 'admin'", src, f"{f.name} reaches the persona editor without an admin check")

    def test_the_post_routes_sit_behind_the_global_csrf_check(self):
        src = (N / "server.py").read_text(encoding="utf-8")
        i_csrf = src.index("if not self.csrf_ok(sess, form):")
        i_route = src.index('if path == "/admin/persona":')
        self.assertLess(i_csrf, i_route)


class HandlerTests(Fresh):
    """The real handler methods, driven with a member session and an admin session."""

    def setUp(self):
        super().setUp()
        from test_nori_navigation import handler
        self.handler = handler

    def test_a_member_is_refused_on_every_route(self):
        for call in ("persona_admin_form", "persona_admin_post", "persona_preview_get"):
            h, sess, env = self.handler("member")
            env["persona_admin"] = Mock()
            args = {"persona_admin_form": (sess,), "persona_admin_post": (sess, {"text": good()}, "save"), "persona_preview_get": (sess, {"target": ["default"]})}[call]
            getattr(h, call)(*args)
            self.assertEqual(h.send.call_args.args[0], 403, call)
            env["persona_admin"].act.assert_not_called()
            env["persona_admin"].render.assert_not_called()
            env["persona_admin"].render_preview.assert_not_called()
        self.assertFalse(persona.PERSONA_PATH.exists())

    def test_an_admin_can_save_and_the_page_confirms_it(self):
        h, sess, env = self.handler("admin")
        env["persona_admin"] = persona_admin
        h.persona_admin_post(sess, {"text": good("edited by an admin")}, "save")
        self.assertEqual(h.send.call_args.args[0], 200)
        self.assertEqual(persona.load_prompt(), "edited by an admin")

    def test_an_admins_rejected_save_shows_the_reason_and_their_draft(self):
        h, sess, env = self.handler("admin")
        env["persona_admin"] = persona_admin
        h.persona_admin_post(sess, {"text": "no markers here, but a lot of effort went into it"}, "save")
        body = h.send.call_args.args[1].decode()
        self.assertIn("not saved", body)
        self.assertIn("a lot of effort went into it", body)
        self.assertFalse(persona.PERSONA_PATH.exists())

    def test_an_unknown_history_name_is_a_404_for_an_admin(self):
        h, sess, env = self.handler("admin")
        env["persona_admin"] = persona_admin
        h.not_found = lambda: h.send(404, b"nf")
        h.persona_preview_get(sess, {"target": ["history"], "name": ["../../secret.key"]})
        self.assertEqual(h.send.call_args.args[0], 404)


class ContextTuningTests(unittest.TestCase):
    """The context-tuning page (Nori already had it; these hold its two public-release properties): sensible defaults for a fresh install, and no tool path to it."""
    KEYS = ("context_window_msgs", "compaction_enabled", "compaction_max_segments", "compaction_budget_tokens", "compaction_session_gap_hours", "peer_recent_cap",
            "peer_recent_window_hours", "memory_max_tokens", "memory_pinned_max_tokens")

    def test_every_default_is_inside_its_own_bounds_so_a_fresh_install_needs_no_tuning(self):
        import config
        for k in self.KEYS:
            self.assertIn(k, config.spec())
        for k, (lo, hi) in config._CONTEXT_BOUNDS.items():
            self.assertLessEqual(lo, config.spec()[k][0], k)
            self.assertLessEqual(config.spec()[k][0], hi, k)

    def test_no_tool_can_change_a_context_tuning_value(self):
        import settings_tool
        self.assertEqual(set(self.KEYS) & set(settings_tool.PEER_SETTINGS_KEYS), set())

    def test_the_page_is_admin_only_in_the_settings_groups(self):
        src = (N / "server.py").read_text(encoding="utf-8")
        for fn in ("context_admin_form", "context_admin_post"):
            i = src.index(f"def {fn}(")
            self.assertIn("if sess['role'] != 'admin'".replace("'", chr(34)), src[i:i + 400], fn)


if __name__ == "__main__":
    unittest.main()

"""Offline image pre-check override: real settings, generation, and UI handlers."""
import ast
import html
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_image_precheck_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts
import chat
import config
import imagegen
import medialog
import store

store.init()
USER = accounts.bootstrap_admin("Tester", "testpass123")
SESSION = dict(user_id=USER["id"], workspace_id=USER["workspace_id"], role="admin", csrf="test")
KEY = "image_content_precheck_enabled"
PROMPT = "a sculpture on a pedestal, no gore"


def handler():
    # Avoid importing server.py's live bootstrap, as in test_nori_chat.py.
    tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
    cls.bases = []
    cls.body = [n for n in cls.body if getattr(n, "name", None) in
                {"media_admin_form", "media_admin_post"}]
    env = dict(config=config, imagegen=imagegen, medialog=medialog,
               esc=html.escape, info_tip=lambda text: text, time=time)
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "server.py", "exec"), env)
    instance = env["Handler"]()
    instance._settings_response = Mock()
    instance.forbidden = Mock()
    return instance


class ImagePrecheckTests(unittest.TestCase):
    def setUp(self):
        self.ws = SESSION["workspace_id"]
        store.write(lambda c: c.execute("DELETE FROM settings WHERE key=?", (KEY,)))
        config.set("workspace", self.ws, "image_gen_enabled", True)
        config.set("workspace", self.ws, "image_daily_cap_usd", 1.0)
        self.provider = patch.object(chat, "openrouter_image", return_value={
            "ok": False, "reason": "blocked this request by content policy"}).start()
        self.addCleanup(patch.stopall)

    def test_default_blocks_both_tools_before_provider(self):
        self.assertTrue(config.get("workspace", self.ws, KEY))
        for generate in (imagegen.generate_selfie_impl, imagegen.imagine_impl):
            result = generate(SESSION, PROMPT)
            self.assertFalse(result["ok"])
            self.assertIn("graphic violence", result["reason"])
        self.provider.assert_not_called()

    def test_disabled_reaches_provider_for_both_tools_and_retains_refusals(self):
        config.set("workspace", self.ws, KEY, False)
        with patch.object(imagegen, "prompt_flags", side_effect=AssertionError("pre-check ran")):
            for generate in (imagegen.generate_selfie_impl, imagegen.imagine_impl):
                result = generate(SESSION, PROMPT)
                self.assertFalse(result["ok"])
                self.assertTrue(result["content_refusal"])
                self.assertIn(imagegen.image_content_rules(self.ws), self.provider.call_args.args[0])
        self.assertEqual(self.provider.call_count, 2)
        self.assertTrue(config.get("workspace", self.ws + 1, KEY))

    def test_disabled_still_obeys_budget_and_generation_toggle(self):
        config.set("workspace", self.ws, KEY, False)
        config.set("workspace", self.ws, "image_daily_cap_usd", 0)
        self.assertIn("budget", imagegen.imagine_impl(SESSION, PROMPT)["reason"])
        config.set("workspace", self.ws, "image_gen_enabled", False)
        self.assertIn("turned off", imagegen.imagine_impl(SESSION, PROMPT)["reason"])
        self.provider.assert_not_called()

    def test_ui_save_reload_and_reenable(self):
        h = handler()
        for value, selected in (("0", "off"), ("1", "on")):
            h.media_admin_post(SESSION, {KEY: value})
            self.assertEqual(config.get("workspace", self.ws, KEY), value == "1")
            rendered = h._settings_response.call_args.args[2]
            field = rendered.split(f"name={KEY}>", 1)[1].split("</select>", 1)[0]
            self.assertIn(f"value={value} selected>{selected}</option>", field)
        imagegen.imagine_impl(SESSION, PROMPT)
        self.provider.assert_not_called()

    def test_members_cannot_view_or_save_override(self):
        h = handler()
        member = dict(SESSION, role="member")
        h.media_admin_form(member)
        h.media_admin_post(member, {KEY: "0"})
        self.assertEqual(h.forbidden.call_count, 2)
        h._settings_response.assert_not_called()
        self.assertTrue(config.get("workspace", self.ws, KEY))


if __name__ == "__main__":
    unittest.main()

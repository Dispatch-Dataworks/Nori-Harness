# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""First-run setup (2026-10-28, firstrun.py): connect a provider, choose a primary model and fallbacks, name her and give her pronouns, then she speaks.
Real SQLite, real providers/models tables, the real page renderers; only the model call (chat.call_for_model) is faked.

Pinned: a fresh admin is sent to /first-run and an instance with history never is; each step is done because the data says so; finishing is refused while
she has no model; her name and pronouns are validated and reach the prompt; the wizard's forms come back to the wizard (not Settings); the first model added
becomes the primary; a model can be tested; no model-callable tool can flip the setup flags; the guide stops asking for a name that setup already settled."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_firstrun_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_firstrun_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"
for _f in ("persona.default.md", "persona.template.md"):
    shutil.copy(ROOT / "prompts" / _f, os.environ["NORI_PROMPTS_DIR"])

import accounts  # noqa: E402
import chat  # noqa: E402
import config  # noqa: E402
import context  # noqa: E402
import conversation  # noqa: E402
import firstrun  # noqa: E402
import models  # noqa: E402
import onboarding  # noqa: E402
import providers  # noqa: E402
import server  # noqa: E402
import settings_tool  # noqa: E402
import store  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Ben", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
_inv_id, _inv_tok = accounts.create_invite(WS, "Gigi", "member", UID)
MID = accounts.accept_invite(_inv_tok, "memberpass123")["id"]
ADMIN = {"user_id": UID, "workspace_id": WS, "role": "admin", "csrf": "csrf-tok"}
MEMBER = {"user_id": MID, "workspace_id": WS, "role": "member", "csrf": "csrf-mem"}


def _reset():
    """A brand-new instance: no history, no providers, no models, nothing set up."""
    store.write(lambda c: c.execute("DELETE FROM messages"))
    store.write(lambda c: c.execute("DELETE FROM onboarding"))
    store.write(lambda c: c.execute("DELETE FROM model_chain"))
    store.write(lambda c: c.execute("UPDATE sub_agents SET model_id=NULL"))
    store.write(lambda c: c.execute("DELETE FROM models"))
    store.write(lambda c: c.execute("DELETE FROM providers"))
    for key in ("setup_complete", "setup_identity", "assistant_pronouns", "assistant_name"):
        store.write(lambda c, k=key: c.execute("DELETE FROM settings WHERE scope='workspace' AND scope_id=? AND key=?", (WS, k)))


def _connect(label="OpenRouter", ptype="openrouter"):
    ok, _ = providers.create_api_key(WS, ptype, label, "sk-test-key", UID)
    assert ok
    return [p for p in providers.list_all(WS) if p["label"] == label][-1]["id"]


def _model(provider_id, alias="Sonnet", name="anthropic/claude-sonnet"):
    ok, _ = models.create(alias, provider_id, name, None)
    assert ok
    return max(m["id"] for m in models.list_all() if m["alias"] == alias)


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

    def html(self) -> str:
        body = self.sent[-1]["body"]
        return body.decode("utf-8") if isinstance(body, bytes) else body


def _page(sess=ADMIN, step="", **kw):
    h = _H()
    h.first_run_page(dict(sess), step=step, **kw)
    return h


class Logic(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_a_fresh_instance_needs_setup(self):
        self.assertTrue(firstrun.needed(WS))
        self.assertEqual(firstrun.next_step(WS), "connect")

    def test_an_instance_with_history_is_never_sent_through_it_and_stays_that_way(self):
        conversation.add_message(UID, "user", "hello")
        self.assertFalse(firstrun.needed(WS))
        self.assertTrue(config.get("workspace", WS, "setup_complete"), "marked complete so the answer can't change later")
        store.write(lambda c: c.execute("DELETE FROM messages"))
        self.assertFalse(firstrun.needed(WS))

    def test_steps_are_done_because_the_data_says_so(self):
        self.assertEqual(firstrun.done(WS), {"connect": False, "models": False, "identity": False})
        pid = _connect()
        self.assertEqual(firstrun.done(WS), {"connect": True, "models": False, "identity": False})
        self.assertEqual(firstrun.next_step(WS), "models")
        models.set_chain(WS, [_model(pid)])
        self.assertEqual(firstrun.done(WS), {"connect": True, "models": True, "identity": False})
        self.assertEqual(firstrun.next_step(WS), "identity")
        firstrun.save_identity(WS, "Iron", "she/her")
        self.assertTrue(all(firstrun.done(WS).values()))

    def test_a_disconnected_or_disabled_provider_does_not_count(self):
        pid = _connect()
        providers.set_enabled(pid, False)
        self.assertFalse(firstrun.has_provider(WS))

    def test_identity_is_validated(self):
        for name, pro in (("", "she/her"), ("x" * (firstrun.NAME_MAX + 1), "she/her"), ("<b>Iron</b>", "she/her"), ("Iron", "xe/xem"), ("Iron", "")):
            ok, msg = firstrun.save_identity(WS, name, pro)
            self.assertFalse(ok, (name, pro))
            self.assertTrue(msg)
        self.assertFalse(config.get("workspace", WS, "setup_identity"))
        self.assertEqual(config.get("workspace", WS, "assistant_name"), "Nori")

    def test_identity_is_saved(self):
        ok, _ = firstrun.save_identity(WS, "  Iron ", "they/them")
        self.assertTrue(ok)
        self.assertEqual(config.get("workspace", WS, "assistant_name"), "Iron")
        self.assertEqual(firstrun.pronouns(WS), "they/them")
        self.assertTrue(config.get("workspace", WS, "setup_identity"))

    def test_pronouns_default_to_she_her(self):
        self.assertEqual(firstrun.pronouns(WS), "she/her")
        self.assertEqual(config.PRONOUNS[0], "she/her")

    def test_finish_is_refused_without_a_model_and_allowed_with_one(self):
        ok, msg = firstrun.finish(WS)
        self.assertFalse(ok)
        self.assertIn("primary model", msg)
        self.assertFalse(config.get("workspace", WS, "setup_complete"))
        models.set_chain(WS, [_model(_connect())])
        self.assertTrue(firstrun.finish(WS)[0])
        self.assertFalse(firstrun.needed(WS))

    def test_skip_stops_the_redirect(self):
        firstrun.skip(WS)
        self.assertFalse(firstrun.needed(WS))

    def test_config_rejects_unknown_pronouns(self):
        with self.assertRaises(ValueError):
            config.set("workspace", WS, "assistant_pronouns", "xe/xem")

    def test_no_model_callable_tool_can_flip_the_setup_flags_or_her_pronouns(self):
        for field in ("setup_complete", "setup_identity", "assistant_pronouns", "assistant_name"):
            out = settings_tool._update_settings_impl(dict(ADMIN), field, "he/him" if field == "assistant_pronouns" else True)
            self.assertIn("error", out, field)
        self.assertFalse(config.get("workspace", WS, "setup_complete"))
        self.assertEqual(firstrun.pronouns(WS), "she/her")


class PromptHeader(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_her_pronouns_are_in_the_name_header(self):
        self.assertIn("Your pronouns are she/her:", context.build_system(UID, "Ben"))
        config.set("workspace", WS, "assistant_pronouns", "they/them")
        prompt = context.build_system(UID, "Ben")
        self.assertIn("Your pronouns are they/them:", prompt)
        self.assertNotIn("she/her", prompt)

    def test_the_name_and_the_pronouns_sit_together(self):
        config.set("workspace", WS, "assistant_name", "Iron")
        self.assertIn("Your name is Iron. Your pronouns are she/her:", context.build_system(UID, "Ben"))


class Redirect(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_a_fresh_admin_is_sent_to_setup(self):
        h = _H()
        self.assertTrue(h._first_run_redirect(dict(ADMIN)))
        self.assertEqual((h.sent[-1]["code"], h.sent[-1]["extra"]["Location"]), (303, "/first-run"))

    def test_a_member_is_not(self):
        h = _H()
        self.assertFalse(h._first_run_redirect(dict(MEMBER)))
        self.assertFalse(h.sent)

    def test_once_finished_or_skipped_nobody_is_redirected(self):
        firstrun.skip(WS)
        h = _H()
        self.assertFalse(h._first_run_redirect(dict(ADMIN)))

    def test_an_instance_with_history_is_not_redirected(self):
        conversation.add_message(UID, "user", "hi")
        h = _H()
        self.assertFalse(h._first_run_redirect(dict(ADMIN)))

    def test_the_no_model_welcome_card_points_at_setup(self):
        h = _H()
        with patch.object(chat, "_resolve_model_chain", return_value=[]):
            h.chat_page(dict(ADMIN))
        self.assertIn("href='/first-run'", h.html())


class Pages(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_a_member_is_bounced_to_the_chat(self):
        h = _page(MEMBER)
        self.assertEqual((h.sent[-1]["code"], h.sent[-1]["extra"]["Location"]), (303, "/"))

    def test_the_first_step_is_connecting_a_provider(self):
        html = _page().html()
        self.assertIn("something to think with", html)
        self.assertIn("class=stepper", html)
        self.assertIn("action='/admin/providers'", html)
        self.assertIn("name=from value=firstrun", html, "its forms come back here, not to Settings")
        self.assertNotIn("href='/first-run?step=models'>Continue", html, "can't continue until something is connected")
        self.assertIn("Skip setup for now", html)

    def test_connecting_a_provider_unlocks_the_next_step(self):
        _connect()
        html = _page(step="connect").html()
        self.assertIn("Continue: choose models", html)

    def test_the_models_step_needs_a_provider_first(self):
        self.assertIn("Connect a provider first", _page(step="models").html())

    def test_the_models_step_warns_until_there_is_a_primary_and_then_a_fallback(self):
        pid = _connect()
        self.assertIn("No primary model yet", _page(step="models").html())
        a = _model(pid, "Sonnet", "a/one")
        models.set_chain(WS, [a])
        html = _page(step="models").html()
        self.assertIn("No fallback yet", html)
        self.assertIn("Continue: name her", html)
        b = _model(pid, "Haiku", "a/two")
        models.set_chain(WS, [a, b])
        self.assertIn("same provider as the primary", _page(step="models").html())
        other = _connect("Direct", "anthropic_api_key")
        c = _model(other, "Direct Sonnet", "claude-sonnet")
        models.set_chain(WS, [a, c])
        self.assertIn("different providers", _page(step="models").html())

    def test_the_models_step_has_browse_test_and_chain_forms_that_return_here(self):
        pid = _connect()
        mid = _model(pid)
        html = _page(step="models").html()
        self.assertIn(f"action='/admin/providers/{pid}/models'", html)
        self.assertIn(f"action='/admin/models/{mid}/test'", html)
        self.assertIn("action='/admin/models/chain'", html)
        self.assertGreaterEqual(html.count("name=from value=firstrun"), 4)

    def test_the_identity_step_has_her_name_and_three_pronoun_choices_with_she_her_selected(self):
        _connect()
        models.set_chain(WS, [_model(_connect("Two"))])
        html = _page(step="identity").html()
        self.assertIn("action='/first-run/identity'", html)
        self.assertIn("name=name", html)
        for p in config.PRONOUNS:
            self.assertIn(f"value='{p}'", html)
        self.assertIn("value='she/her' checked", html)
        self.assertNotIn("value='he/him' checked", html)
        self.assertIn("Save, and meet her", html)

    def test_the_identity_step_without_a_model_cannot_be_submitted(self):
        html = _page(step="identity").html()
        self.assertIn("Choose a primary model first", html)
        self.assertIn("disabled", html)

    def test_the_default_step_follows_the_data(self):
        self.assertIn("something to think with", _page().html())
        pid = _connect()
        self.assertIn("Pick her primary model", _page().html())
        models.set_chain(WS, [_model(pid)])
        self.assertIn("What should she be called?", _page().html())

    def test_the_stepper_marks_finished_steps(self):
        pid = _connect()
        models.set_chain(WS, [_model(pid)])
        html = _page(step="identity").html()
        self.assertEqual(html.count("class='cur"), 1)
        self.assertGreaterEqual(html.count(" ok'"), 2)

    def test_the_about_tab_links_back_to_setup_for_the_admin_only(self):
        self.assertIn("href='/first-run'", self._about(ADMIN))
        self.assertNotIn("href='/first-run'", self._about(MEMBER))

    @staticmethod
    def _about(sess):
        h = _H()
        h._settings_response = lambda s, tab, body, **kw: h.sent.append({"code": 200, "body": body})
        h.settings_page(dict(sess), "about")
        return h.html()


class Posts(unittest.TestCase):
    def setUp(self):
        _reset()

    def _identity(self, sess, **form):
        h = _H()
        h.first_run_identity_post(dict(sess), form)
        return h

    def test_saving_a_name_and_pronouns_finishes_setup_and_goes_to_the_chat(self):
        models.set_chain(WS, [_model(_connect())])
        h = self._identity(ADMIN, name="Iron", pronouns="she/her")
        self.assertEqual((h.sent[-1]["code"], h.sent[-1]["extra"]["Location"]), (303, "/"))
        self.assertEqual(config.get("workspace", WS, "assistant_name"), "Iron")
        self.assertTrue(config.get("workspace", WS, "setup_complete"))
        self.assertFalse(firstrun.needed(WS))

    def test_without_a_model_it_saves_her_name_but_does_not_finish_and_sends_the_admin_back_to_choose_one(self):
        h = self._identity(ADMIN, name="Iron", pronouns="she/her")
        self.assertEqual(h.sent[-1]["code"], 200)
        self.assertIn("primary model first", h.html())
        self.assertFalse(config.get("workspace", WS, "setup_complete"))
        self.assertEqual(config.get("workspace", WS, "assistant_name"), "Iron", "the name isn't lost")

    def test_a_bad_name_re_renders_the_form_with_the_reason(self):
        models.set_chain(WS, [_model(_connect())])
        h = self._identity(ADMIN, name="<script>", pronouns="she/her")
        self.assertEqual(h.sent[-1]["code"], 200)
        self.assertIn("a name is 1-", h.html())
        self.assertFalse(config.get("workspace", WS, "setup_complete"))

    def test_a_member_cannot_do_any_of_it(self):
        models.set_chain(WS, [_model(_connect())])
        self.assertEqual(self._identity(MEMBER, name="Hacked", pronouns="he/him").sent[-1]["code"], 403)
        h = _H()
        h.first_run_skip_post(dict(MEMBER), {})
        self.assertEqual(h.sent[-1]["code"], 403)
        self.assertEqual(config.get("workspace", WS, "assistant_name"), "Nori")
        self.assertFalse(config.get("workspace", WS, "setup_complete"))

    def test_skip(self):
        h = _H()
        h.first_run_skip_post(dict(ADMIN), {})
        self.assertEqual((h.sent[-1]["code"], h.sent[-1]["extra"]["Location"]), (303, "/"))
        self.assertTrue(config.get("workspace", WS, "setup_complete"))

    def test_a_provider_added_from_the_wizard_comes_back_to_the_wizard(self):
        h = _H()
        h.providers_create_post(dict(ADMIN), {"type": "openrouter", "label": "OR", "api_key": "sk-x", "from": "firstrun", "step": "connect"})
        self.assertIn("class=stepper", h.html())
        self.assertIn("Continue: choose models", h.html())

    def test_the_same_form_from_settings_comes_back_to_settings(self):
        h = _H()
        h._settings_response = lambda sess, tab, body, **kw: h.sent.append({"code": 200, "body": f"SETTINGS:{tab}:{body}"})
        h.providers_create_post(dict(ADMIN), {"type": "openrouter", "label": "OR", "api_key": "sk-x"})
        self.assertTrue(h.html().startswith("SETTINGS:models:"))
        self.assertNotIn("class=stepper", h.html())

    def test_the_first_model_added_in_the_wizard_becomes_her_primary(self):
        pid = _connect()
        h = _H()
        h.models_create_post(dict(ADMIN), {"alias": "Sonnet", "provider_id": str(pid), "model_name": "a/one", "from": "firstrun", "step": "models"})
        self.assertEqual([m["alias"] for m in models.get_chain(WS)], ["Sonnet"])
        self.assertIn("set as her primary", h.html())

    def test_a_second_model_does_not_displace_the_primary(self):
        pid = _connect()
        first = _model(pid, "Sonnet", "a/one")
        models.set_chain(WS, [first])
        h = _H()
        h.models_create_post(dict(ADMIN), {"alias": "Haiku", "provider_id": str(pid), "model_name": "a/two", "from": "firstrun", "step": "models"})
        self.assertEqual([m["alias"] for m in models.get_chain(WS)], ["Sonnet"])

    def test_in_settings_adding_a_model_does_not_choose_a_primary_for_you(self):
        pid = _connect()
        h = _H()
        h._settings_response = lambda *a, **kw: h.sent.append({"code": 200, "body": ""})
        h.models_create_post(dict(ADMIN), {"alias": "Sonnet", "provider_id": str(pid), "model_name": "a/one"})
        self.assertEqual(models.get_chain(WS), [])

    def test_the_chain_saved_in_the_wizard_comes_back_to_the_wizard(self):
        pid = _connect()
        a, b = _model(pid, "Sonnet", "a/one"), _model(pid, "Haiku", "a/two")
        h = _H()
        h.models_chain_post(dict(ADMIN), {"primary": str(a), "fallback_0": str(b), "from": "firstrun", "step": "models"})
        self.assertEqual([m["alias"] for m in models.get_chain(WS)], ["Sonnet", "Haiku"])
        self.assertIn("class=stepper", h.html())

    def _test(self, mid, **patched):
        h = _H()
        with patch.object(chat, "call_for_model", **patched):
            h.models_test_post(dict(ADMIN), str(mid), {"from": "firstrun", "step": "models"})
        return h.html()

    def test_a_model_that_answers_is_reported(self):
        mid = _model(_connect())
        html = self._test(mid, return_value={"content": "ready", "usage": {}})
        self.assertIn("Sonnet answered: ready", html)

    def test_a_model_that_fails_is_reported_with_the_reason(self):
        mid = _model(_connect())
        html = self._test(mid, side_effect=chat.ModelError("401 invalid key"))
        self.assertIn("did not answer", html)
        self.assertIn("401 invalid key", html)

    def test_testing_a_model_whose_provider_is_gone_says_so(self):
        pid = _connect()
        mid = _model(pid)
        providers.delete(pid)
        self.assertIn("isn&#x27;t connected", self._test(mid, return_value={"content": "x"}).replace("isn't", "isn&#x27;t"))

    def test_only_the_admin_can_test_a_model(self):
        mid = _model(_connect())
        h = _H()
        with patch.object(chat, "call_for_model") as call:
            h.models_test_post(dict(MEMBER), str(mid), {})
        call.assert_not_called()
        self.assertEqual(h.sent[-1]["code"], 403)


class Guide(unittest.TestCase):
    """The getting-to-know-you chat doesn't ask for what setup already settled."""

    def setUp(self):
        _reset()

    def _guide(self):
        return onboarding.guide(accounts.get_user(UID))

    def test_without_setup_she_asks_what_to_call_her(self):
        text = self._guide()
        self.assertIn("what they'd like to call you -> `onboarding_set_name`", text)
        self.assertIn("ask what they'd like to call you", text)
        self.assertNotIn("settled in first-run setup", text)

    def test_after_setup_her_name_and_pronouns_are_settled_and_not_asked_again(self):
        firstrun.save_identity(WS, "Iron", "she/her")
        text = self._guide()
        self.assertIn("settled in first-run setup as 'Iron', she/her", text)
        self.assertIn("already settled, so don't ask about them", text)
        self.assertIn("they already chose your name and pronouns in setup", text)
        self.assertNotIn("what they'd like to call you -> `onboarding_set_name`", text)

    def test_she_still_asks_what_they_expect_of_her(self):
        self.assertIn("what they expect you to do on your own", self._guide())
        self.assertIn("simply be their assistant", self._guide())

    def test_a_member_guide_never_mentions_her_name_rules(self):
        firstrun.save_identity(WS, "Iron", "she/her")
        text = onboarding.guide(accounts.get_user(MID))
        self.assertNotIn("first-run setup", text)


if __name__ == "__main__":
    unittest.main()

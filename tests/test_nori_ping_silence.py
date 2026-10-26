# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Staying quiet on a turn she started herself (2026-10-21). A ping used to force "say something brief about it now", so with nothing to say she
carried on her own last message, as if talking to herself. Now the ping prompt makes speaking optional, and replying with the silence token sends
nothing -- enforced here in the harness, not left to her manners. Real scheduler/conversation/SQLite; only the model (chat.run) is faked."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_ping_silence_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_ping_silence_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import chat  # noqa: E402
import config  # noqa: E402
import conversation  # noqa: E402
import own_output  # noqa: E402
import scheduler  # noqa: E402
import store  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Tester", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
USER = {"id": UID, "role": "admin", "workspace_id": WS, "display_name": "Tester"}
USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0}


def _model(text):
    return Mock(return_value={"text": text, "usage": USAGE, "hit_round_limit": False, "dispatch_meta": None, "tool_calls": []})


def _count(kind):
    return store.read(lambda c: c.execute("SELECT COUNT(*) FROM messages WHERE user_id=? AND kind=?", (UID, kind)).fetchone())[0]


class IsSilentTests(unittest.TestCase):
    def test_the_token_and_the_ways_a_model_wraps_it(self):
        for t in ("[silent]", "[Silent]", "**[silent]**", "`SILENT`", "NO_ACTION", "no action.", "(no message)", "Stay quiet.", "  \n[silent]\n ",
                  "[silent] nothing new since the last time", ""):
            self.assertTrue(own_output.is_silent(t), repr(t))

    def test_the_apps_own_no_reply_fallback_is_no_message_either(self):
        self.assertTrue(own_output.is_silent("(no reply — the model returned nothing)"))

    def test_real_sentences_are_not_silence(self):
        for t in ("Silent night, and the dinner isn't planned.", "I stayed silent about it earlier.", "Dinner isn't planned yet -- want ideas?",
                  "No action needed on the Work event, but the concert starts at 7."):
            self.assertFalse(own_output.is_silent(t), repr(t))

    def test_non_strings_are_not_silence(self):
        self.assertFalse(own_output.is_silent(None))
        self.assertFalse(own_output.is_silent(3))


class PingTests(unittest.TestCase):
    def setUp(self):
        store.write(lambda c: c.execute("DELETE FROM messages"))
        conversation.add_message(UID, "assistant", "Earlier reply about the porch lab.")

    def test_a_ping_she_stays_quiet_on_sends_and_stores_nothing(self):
        run = _model("[silent]")
        with patch.object(chat, "run", run):
            self.assertEqual(scheduler._send_proactive(USER, "there's no dinner planned for tonight yet"), "silent")
        self.assertEqual(_count("proactive"), 0)
        self.assertEqual(store.read(lambda c: c.execute("SELECT COUNT(*) FROM messages WHERE user_id=?", (UID,)).fetchone())[0], 1,
                         "nothing was added to the conversation")

    def test_the_apps_no_reply_fallback_is_not_sent_to_them(self):
        with patch.object(chat, "run", _model("(no reply — the model returned nothing)")):
            self.assertEqual(scheduler._send_proactive(USER, "x"), "silent")
        self.assertEqual(_count("proactive"), 0)

    def test_a_ping_she_speaks_on_is_stored_with_its_reason(self):
        with patch.object(chat, "run", _model("No dinner planned tonight -- want me to suggest something?")):
            self.assertEqual(scheduler._send_proactive(USER, "there's no dinner planned for tonight yet"), "sent")
        self.assertEqual(_count("proactive"), 1)

    def test_the_prompt_makes_speaking_optional_and_names_the_way_out(self):
        run = _model("[silent]")
        with patch.object(chat, "run", run):
            scheduler._send_proactive(USER, "there's no dinner planned for tonight yet")
        prompt = run.call_args.kwargs["extra_message"]["content"]
        self.assertIn("there's no dinner planned for tonight yet", prompt)
        self.assertIn("[silent]", prompt)
        self.assertIn("worth interrupting", prompt)
        self.assertIn("own last message", prompt, "following up on herself is named as a reason to stay quiet")
        self.assertNotIn("Say something brief", prompt, "the old instruction forced a message")

    def test_a_silent_ping_still_spends_the_signals_cooldown_so_it_is_not_asked_every_cycle(self):
        config.set("user", UID, "ping_enabled", True)
        fired = Mock()
        reasons = {"reason": "r", "key": "meal_planning", "dedup_token": ""}
        with patch.multiple(scheduler, _maybe_run_backup=Mock(), _maybe_run_integration_health=Mock(), _maybe_reflect=Mock(), _maybe_compact=Mock(), _maybe_check_updates=Mock(),
                            _check_due_schedules=Mock(), _check_due_reminders=Mock(), _recently_active=Mock(return_value=False),
                            _within_window=Mock(return_value=True), outstanding_reason=Mock(return_value=reasons), mark_signal_fired=fired), \
                patch.object(scheduler.turns, "in_flight", return_value=False), \
                patch.object(scheduler, "_send_proactive", return_value="silent"):
            self.assertEqual(scheduler.run_cycle_once(), 0, "nothing was sent")
        fired.assert_called_once_with(UID, "meal_planning", "")

    def test_a_queued_ping_does_not_spend_the_cooldown(self):
        config.set("user", UID, "ping_enabled", True)
        fired = Mock()
        reasons = {"reason": "r", "key": "meal_planning", "dedup_token": ""}
        with patch.multiple(scheduler, _maybe_run_backup=Mock(), _maybe_run_integration_health=Mock(), _maybe_reflect=Mock(), _maybe_compact=Mock(), _maybe_check_updates=Mock(),
                            _check_due_schedules=Mock(), _check_due_reminders=Mock(), _recently_active=Mock(return_value=False),
                            _within_window=Mock(return_value=True), outstanding_reason=Mock(return_value=reasons), mark_signal_fired=fired), \
                patch.object(scheduler.turns, "in_flight", return_value=False), \
                patch.object(scheduler, "_send_proactive", return_value="skipped_turn_in_progress"):
            scheduler.run_cycle_once()
        fired.assert_not_called()


class ScheduledTaskTests(unittest.TestCase):
    SCHED = {"id": 7, "name": "check the garage", "instruction": "Look at the garage sensor; tell me only if the door is open.",
             "deliver_to": "user", "deliver_peer_id": None, "required_tool": None, "schedule_type": "interval", "interval_min": 60,
             "time_hour": None, "time_minute": None, "user_id": UID}

    def setUp(self):
        store.write(lambda c: c.execute("DELETE FROM messages"))

    def _fire(self, text, **over):
        run = _model(text)
        rec, adv = Mock(), Mock()
        with patch.object(chat, "run", run), patch.object(scheduler.schedules, "record_run", rec), patch.object(scheduler.schedules, "advance", adv):
            scheduler._fire_schedule(USER, {**self.SCHED, **over})
        return run, rec, adv

    def test_a_conditional_task_whose_condition_is_not_met_stays_quiet_and_still_counts_as_run(self):
        _run, rec, adv = self._fire("[silent]")
        self.assertEqual(_count("scheduled"), 0)
        self.assertEqual(rec.call_args.kwargs["status"], "ok")
        self.assertEqual(adv.call_args.kwargs["status"], "ok")

    def test_a_task_with_something_to_say_says_it(self):
        self._fire("The garage door is open.")
        self.assertEqual(_count("scheduled"), 1)

    def test_the_task_prompt_offers_silence_only_when_she_speaks_to_him(self):
        run, _r, _a = self._fire("x")
        self.assertIn("[silent]", run.call_args.kwargs["extra_message"]["content"])
        self.assertIn("is not conditional: say it", run.call_args.kwargs["extra_message"]["content"])
        run, _r, _a = self._fire("x", deliver_to="none")
        self.assertNotIn("[silent]", run.call_args.kwargs["extra_message"]["content"])


DISPATCH = {"provider_type": "openai_oauth", "provider_label": "OpenAI", "model_alias": "GPT", "model_name": "gpt-x", "chain_position": "primary",
            "chain_length": 2, "request_id": "resp_1", "reasoning": None, "latency_ms": 5, "failed_attempts": []}


def _model_with_dispatch(text):
    return Mock(return_value={"text": text, "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": None}, "hit_round_limit": False,
                              "dispatch_meta": dict(DISPATCH), "tool_calls": [{"round": 1, "name": "set_emotion", "args": {"state": "x"}}]})


def _last_meta(kind):
    import json
    row = store.read(lambda c: c.execute("SELECT meta FROM messages WHERE user_id=? AND kind=? ORDER BY id DESC LIMIT 1", (UID, kind)).fetchone())
    return json.loads(row["meta"])


class InfoPanelTests(unittest.TestCase):
    """2026-10-22: messages she started herself had no (i) button, so there was nowhere to see what triggered them or which model wrote them."""

    def setUp(self):
        store.write(lambda c: c.execute("DELETE FROM messages"))

    def test_a_ping_message_carries_the_info_panel_with_its_trigger(self):
        with patch.object(chat, "run", _model_with_dispatch("Flea and tick meds in 12 minutes.")):
            scheduler._send_proactive(USER, "a calendar event starting in about 12 min: \"Flea and tick meds\"")
        meta = _last_meta("proactive")
        self.assertEqual(meta["reason"], "a calendar event starting in about 12 min: \"Flea and tick meds\"")
        d = meta["debug"]
        self.assertEqual(d["trigger"], meta["reason"])
        self.assertEqual((d["provider_type"], d["model_name"], d["request_id"]), ("openai_oauth", "gpt-x", "resp_1"))
        self.assertEqual((d["prompt_tokens"], d["completion_tokens"], d["cost_unavailable"]), (10, 2, True))
        self.assertEqual(d["tool_calls"][0]["name"], "set_emotion")
        self.assertEqual(meta["prompt_tokens"], 10, "the existing cost fields are unchanged")

    def test_the_panel_text_names_the_trigger_first(self):
        import server
        with patch.object(chat, "run", _model_with_dispatch("x")):
            scheduler._send_proactive(USER, "overdue tasks: Helen's desk")
        text = server._debug_text(_last_meta("proactive")["debug"])
        self.assertTrue(text.startswith("Triggered by: overdue tasks: Helen's desk"), text)
        self.assertIn("Model: GPT (gpt-x)", text)
        self.assertIn("Cost: unavailable", text)

    def test_a_panel_with_only_a_trigger_renders_without_provider_noise(self):
        import server
        text = server._debug_text({"trigger": "peer asked", "ts": 1792613315})
        self.assertIn("Triggered by: peer asked", text)
        self.assertNotIn("Provider", text)
        self.assertNotIn("None", text)

    def test_a_chat_reply_panel_is_unchanged(self):
        import server
        text = server._debug_text({**DISPATCH, "ts": 1792613315, "cost_unavailable": True, "prompt_tokens": 3, "completion_tokens": 4, "tool_calls": []})
        self.assertNotIn("Triggered by", text)
        self.assertIn("Provider: OpenAI (openai_oauth)", text)

    def test_scheduled_and_reminder_messages_get_it_too(self):
        sched = {**ScheduledTaskTests.SCHED, "instruction": "Say good morning."}
        with patch.object(chat, "run", _model_with_dispatch("Good morning.")), patch.object(scheduler.schedules, "record_run"),                 patch.object(scheduler.schedules, "advance"):
            scheduler._fire_schedule(USER, sched)
        d = _last_meta("scheduled")["debug"]
        self.assertEqual(d["trigger"], "schedule #7 (check the garage)")
        self.assertEqual(_last_meta("scheduled")["schedule_id"], 7)

    def test_a_turn_with_no_model_call_and_no_trigger_has_no_panel(self):
        import conversation as cv
        self.assertIsNone(cv.debug_panel({"usage": USAGE}, {}))
        self.assertEqual(cv.turn_meta({"usage": USAGE}).keys(), cv.cost_meta(USAGE).keys())


if __name__ == "__main__":
    unittest.main()

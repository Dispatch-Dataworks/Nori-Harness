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
        with patch.multiple(scheduler, _maybe_run_backup=Mock(), _maybe_run_integration_health=Mock(), _maybe_reflect=Mock(), _maybe_compact=Mock(),
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
        with patch.multiple(scheduler, _maybe_run_backup=Mock(), _maybe_run_integration_health=Mock(), _maybe_reflect=Mock(), _maybe_compact=Mock(),
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


if __name__ == "__main__":
    unittest.main()

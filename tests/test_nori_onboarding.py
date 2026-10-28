# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Onboarding (2026-10-26, onboarding.py): she introduces herself, asks, and sets herself up. Real SQLite, real persona files in a scratch prompts
directory, real tool registry; only the model (chat.run) is faked.

Pinned: an upgraded instance is never greeted; the guide steers turns only while onboarding is active and reflects real state; her tools exist only
while it is active, and the shared ones are admin-only; the persona is a draft that cannot be applied until the user has replied to it, and the old one
is kept; members get the short version; the chat page waits for a model and then has her speak first; it can't nag forever."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_onboarding_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_onboarding_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"
import shutil  # noqa: E402

for _f in ("persona.default.md", "persona.template.md"):      # what the image ships into the prompts volume
    shutil.copy(Path(__file__).resolve().parent.parent / "prompts" / _f, os.environ["NORI_PROMPTS_DIR"])

import accounts  # noqa: E402
import chat  # noqa: E402
import config  # noqa: E402
import conversation  # noqa: E402
import onboarding  # noqa: E402
import persona  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402
import tools  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Ben", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
_inv_id, _inv_tok = accounts.create_invite(WS, "Gigi", "member", UID)
_member = accounts.accept_invite(_inv_tok, "memberpass123")
MID = _member["id"]
ADMIN = {"user_id": UID, "workspace_id": WS, "role": "admin", "csrf": "csrf-tok"}
MEMBER = {"user_id": MID, "workspace_id": WS, "role": "member", "csrf": "csrf-mem"}
CHAIN = [{"model_name": "x", "provider": {"type": "openrouter"}}]
USAGE = {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0}

TOOL_NAMES = ("onboarding_set_name", "onboarding_set_timezone", "onboarding_propose_persona", "onboarding_tour", "onboarding_finish")
PERSONA = "You are {{ASSISTANT_NAME}}, dry and warm. Short answers. Tease gently, never when it's urgent."


def _user(uid):
    return accounts.get_user(uid)


def _reset(*uids):
    for uid in uids:
        store.write(lambda c, u=uid: c.execute("DELETE FROM onboarding WHERE user_id=?", (u,)))
        store.write(lambda c, u=uid: c.execute("DELETE FROM messages WHERE user_id=?", (u,)))
        store.write(lambda c, u=uid: c.execute("DELETE FROM memory_events WHERE user_id=?", (u,)))
    config.set("workspace", WS, "assistant_name", "Nori")
    config.set("user", UID, "timezone", "America/New_York")
    # No live persona file and no history: she is on the shipped default, as on a fresh instance.
    live = persona._doc.path
    if live.is_file():
        live.unlink()
    hist = persona._doc.history_dir
    if hist.is_dir():
        for f in hist.glob("*.md"):
            f.unlink()


def _active_names(sess):
    return {s["function"]["name"] for s in tools.active_schemas(sess) if s["function"]["name"].startswith("onboarding_")}


class State(unittest.TestCase):
    def setUp(self):
        _reset(UID, MID)

    def test_a_new_user_starts_pending(self):
        self.assertEqual(onboarding.status(UID), "pending")

    def test_anyone_with_history_is_never_greeted(self):
        conversation.add_message(UID, "user", "hello there")
        self.assertEqual(onboarding.status(UID), "done")
        self.assertFalse(onboarding.should_begin(UID, WS))
        self.assertIsNone(onboarding.guide_message(_user(UID)))

    def test_restart_makes_it_pending_again_with_fresh_steps(self):
        conversation.add_message(UID, "user", "hi")
        onboarding.status(UID)
        onboarding._mark(UID, name=True)
        onboarding.request_restart(UID)
        self.assertEqual(onboarding.status(UID), "pending")
        self.assertFalse(onboarding.state(UID)["steps"]["name"])

    def test_start_activates_and_drops_an_old_draft(self):
        onboarding._save(UID, status="pending", draft_persona="x", draft_msg_id=1)
        onboarding.start(UID)
        row = onboarding.state(UID)
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["draft_persona"])
        self.assertTrue(onboarding.is_active(UID))

    def test_it_ends_by_itself_after_three_days(self):
        onboarding.start(UID)
        onboarding._save(UID, started_ts=time.time() - onboarding.MAX_ACTIVE_S - 60)
        self.assertFalse(onboarding.is_active(UID))
        self.assertEqual(onboarding.status(UID), "done")

    def test_it_ends_by_itself_after_too_many_replies(self):
        onboarding.start(UID)
        for _ in range(onboarding.MAX_REPLIES + 1):
            conversation.add_message(UID, "assistant", "x")
        self.assertFalse(onboarding.is_active(UID))

    def test_should_begin_needs_a_model(self):
        with patch.object(chat, "_resolve_model_chain", return_value=[]):
            self.assertFalse(onboarding.should_begin(UID, WS))
        with patch.object(chat, "_resolve_model_chain", return_value=CHAIN):
            self.assertTrue(onboarding.should_begin(UID, WS))
        onboarding.start(UID)
        with patch.object(chat, "_resolve_model_chain", return_value=CHAIN):
            self.assertFalse(onboarding.should_begin(UID, WS), "already started")

    def test_finish_and_skip(self):
        onboarding.start(UID)
        onboarding.finish(UID, skipped=True)
        self.assertEqual(onboarding.status(UID), "skipped")
        self.assertFalse(onboarding.is_active(UID))


class Guide(unittest.TestCase):
    def setUp(self):
        _reset(UID, MID)
        onboarding.start(UID)
        onboarding.start(MID)

    def test_the_admin_guide_covers_name_persona_and_the_tour_and_reports_state(self):
        g = onboarding.guide(_user(UID))
        for needle in ("Ben", "Her name: not settled yet (currently 'Nori')", "Their timezone: not confirmed", "0 thing(s) saved", "Her persona: not started",
                       "onboarding_set_name", "onboarding_propose_persona", "Apply and Discard", "You cannot apply it", "{{ASSISTANT_NAME}}",
                       "onboarding_tour", "onboarding_finish", "skipped=true", "remember"):
            self.assertIn(needle, g, needle)

    def test_the_member_guide_is_the_short_version(self):
        g = onboarding.guide(_user(MID))
        self.assertIn("Gigi", g)
        self.assertIn("household member", g)
        self.assertNotIn("onboarding_set_name", g)
        self.assertNotIn("onboarding_propose_persona", g)
        self.assertNotIn("Her persona:", g)
        self.assertIn("onboarding_set_timezone", g)

    def test_the_checklist_follows_real_state(self):
        onboarding._mark(UID, name=True, timezone=True, tour=["memory", "pings"])
        config.set("workspace", WS, "assistant_name", "Mochi")
        store.write(lambda c: c.execute("INSERT INTO memory_events(ts, memory_id, user_id, action, actor) VALUES (?,?,?,?,?)", (time.time() + 1, 1, UID, "add", "user")))
        g = onboarding.guide(_user(UID))
        self.assertIn("settled as 'Mochi'", g)
        self.assertIn("confirmed as America/New_York", g)
        self.assertIn("1 thing(s) saved", g)
        self.assertIn("Tour: memory, pings", g)

    def test_the_persona_line_shows_draft_applied_discarded_and_an_existing_custom_persona(self):
        onboarding._save(UID, draft_persona="x")
        self.assertIn("a draft is on screen as a card", onboarding.guide(_user(UID)))
        onboarding.settle_draft(UID, "applied")
        self.assertIn("Her persona: applied by them", onboarding.guide(_user(UID)))
        onboarding.settle_draft(UID, "discarded")
        self.assertIn("they discarded your last draft", onboarding.guide(_user(UID)))
        onboarding._mark(UID, persona=None)
        self.assertIn("refine it rather than replace it", onboarding.guide(_user(UID), persona_customised=True))
        self.assertIn("still on the shipped default", onboarding.guide(_user(UID), persona_customised=False))

    def test_the_kickoff_asks_her_to_speak_first(self):
        msg = onboarding.kickoff_message(_user(UID))
        self.assertEqual(msg["role"], "system")
        self.assertIn("Nobody has said anything yet", msg["content"])
        self.assertIn("introduce yourself", msg["content"])
        self.assertNotIn("Nobody has said anything yet", onboarding.guide(_user(UID)))

    def test_guide_message_only_while_active_and_activates_a_pending_user_who_is_already_talking(self):
        onboarding.request_restart(UID)
        with patch.object(chat, "_resolve_model_chain", return_value=[]):
            self.assertIsNone(onboarding.guide_message(_user(UID)), "no model: she can't be steering anything")
        with patch.object(chat, "_resolve_model_chain", return_value=CHAIN):
            m = onboarding.guide_message(_user(UID))
        self.assertEqual(m["role"], "system")
        self.assertEqual(onboarding.status(UID), "active")
        onboarding.finish(UID)
        self.assertIsNone(onboarding.guide_message(_user(UID)))


class Tools(unittest.TestCase):
    def setUp(self):
        _reset(UID, MID)

    def _call(self, tool_name, sess=ADMIN, **args):
        return tools.dispatch(tool_name, args, sess)

    def test_her_tools_exist_only_while_it_is_active_and_the_shared_ones_only_for_the_admin(self):
        self.assertEqual(_active_names(ADMIN), set(), "pending: nothing yet")
        onboarding.start(UID)
        onboarding.start(MID)
        self.assertEqual(_active_names(ADMIN), set(TOOL_NAMES))
        self.assertEqual(_active_names(MEMBER), {"onboarding_set_timezone", "onboarding_tour", "onboarding_finish"})
        onboarding.finish(UID)
        self.assertEqual(_active_names(ADMIN), set())

    def test_a_member_cannot_call_the_admin_tools_even_directly(self):
        onboarding.start(MID)
        for name, args in (("onboarding_set_name", {"name": "Hacked"}), ("onboarding_propose_persona", {"text": PERSONA})):
            out = self._call(name, MEMBER, **args)
            self.assertIn("error", out, name)
        self.assertEqual(config.get("workspace", WS, "assistant_name"), "Nori")

    def test_set_name(self):
        onboarding.start(UID)
        for bad in ("", "   ", "x" * 41, "two\nlines", "<b>", "{{x}}"):
            self.assertIn("error", self._call("onboarding_set_name", ADMIN, name=bad), repr(bad))
        self.assertEqual(config.get("workspace", WS, "assistant_name"), "Nori")
        out = self._call("onboarding_set_name", ADMIN, name="  Mochi ")
        self.assertEqual(out["name"], "Mochi")
        self.assertEqual(config.get("workspace", WS, "assistant_name"), "Mochi")
        self.assertTrue(onboarding.state(UID)["steps"]["name"])

    def test_set_timezone(self):
        onboarding.start(UID)
        self.assertIn("error", self._call("onboarding_set_timezone", ADMIN, timezone="Mars/Olympus"))
        self.assertEqual(config.get("user", UID, "timezone"), "America/New_York")
        out = self._call("onboarding_set_timezone", ADMIN, timezone="Europe/London")
        self.assertTrue(out["ok"])
        self.assertEqual(config.get("user", UID, "timezone"), "Europe/London")
        self.assertTrue(onboarding.state(UID)["steps"]["timezone"])

    def test_tour_lists_stops_and_gives_details_with_live_state(self):
        onboarding.start(UID)
        onboarding.start(MID)
        admin_keys = {s["stop"] for s in self._call("onboarding_tour", ADMIN)["stops"]}
        member_keys = {s["stop"] for s in self._call("onboarding_tour", MEMBER)["stops"]}
        self.assertTrue({"computer_use", "models", "persona", "backups", "updates"} <= admin_keys)
        self.assertTrue(member_keys and not member_keys & {"computer_use", "models", "persona", "backups", "updates", "web"})
        self.assertIn("memory", member_keys)
        config.set("user", UID, "ping_enabled", False)
        d = self._call("onboarding_tour", ADMIN, stop="pings")
        self.assertEqual((d["title"], d["where"]), ("Pings", "Settings -> Pings"))
        self.assertTrue(d["current_state"].startswith("off"))
        self.assertIn("pings", onboarding.state(UID)["steps"]["tour"])
        self.assertTrue(next(s for s in self._call("onboarding_tour", ADMIN)["stops"] if s["stop"] == "pings")["shown"])
        self.assertIn("error", self._call("onboarding_tour", MEMBER, stop="computer_use"), "an admin stop isn't a member's")
        self.assertIn("error", self._call("onboarding_tour", ADMIN, stop="nonsense"))
        config.set("user", UID, "ping_enabled", True)

    def test_the_tour_reports_how_many_things_she_remembers_through_the_configured_backend(self):
        onboarding.start(UID)
        self.assertEqual(self._call("onboarding_tour", ADMIN, stop="memory")["current_state"], "0 remembered")
        tools.dispatch("remember", {"type": "preference", "value": "likes short answers"}, ADMIN)
        self.assertEqual(self._call("onboarding_tour", ADMIN, stop="memory")["current_state"], "1 remembered")

    def test_the_tour_list_carries_a_line_on_each_stop_so_she_never_has_to_guess(self):
        onboarding.start(UID)
        stops = self._call("onboarding_tour", ADMIN)["stops"]
        self.assertTrue(all(s["what"] and s["where"] for s in stops))
        self.assertIn("Android app", next(s for s in stops if s["stop"] == "devices")["what"])

    def test_finish_closes_it(self):
        onboarding.start(UID)
        self._call("onboarding_finish", ADMIN)
        self.assertEqual(onboarding.status(UID), "done")
        onboarding.start(UID)
        self._call("onboarding_finish", ADMIN, skipped=True)
        self.assertEqual(onboarding.status(UID), "skipped")


class PersonaDraft(unittest.TestCase):
    """She can only PROPOSE: the draft is stored inert and shown as a card; the admin's authenticated POST is the only way it becomes her persona."""

    def setUp(self):
        _reset(UID, MID)
        onboarding.start(UID)
        self.before = persona.load_file()

    def _call(self, tool_name, sess=ADMIN, **args):
        return tools.dispatch(tool_name, args, sess)

    def _post(self, sess, action):
        h = _H()
        h.forbidden = lambda: h.sent.append({"code": 403})
        h.onboarding_persona_post(dict(sess), {"action": action})
        return h

    def test_there_is_no_tool_that_applies_or_reads_the_persona(self):
        names = {s["function"]["name"] for s in tools.active_schemas(ADMIN)}
        self.assertNotIn("onboarding_apply_persona", names)
        self.assertNotIn("onboarding_read_persona", names)
        self.assertEqual({n for n in names if "persona" in n}, {"onboarding_propose_persona"})

    def test_proposals_are_checked(self):
        for text, frag in (("", "empty"), ("x" * (onboarding.PERSONA_BODY_MAX + 1), "too long"),
                           ("You are {{ASSISTANT_NAME}}.\n=== PROMPT ENDS ===", "marker"), ("You are Nori, dry.", "{{ASSISTANT_NAME}}")):
            out = self._call("onboarding_propose_persona", text=text)
            self.assertIn(frag, out.get("error", ""), text[:20])
        self.assertIsNone(onboarding.get_draft(UID))

    def test_a_proposal_is_an_inert_draft_shown_as_a_card_and_changes_nothing(self):
        out = self._call("onboarding_propose_persona", text=PERSONA)
        self.assertTrue(out["ok"])
        self.assertIn("You can't apply it", out["next"])
        self.assertEqual(persona.load_file(), self.before, "the live persona is untouched")
        self.assertEqual(onboarding.get_draft(UID), PERSONA)
        card = onboarding.draft_card_id(UID)
        row = next(m for m in conversation.recent(UID) if m["id"] == card)
        self.assertEqual((row["kind"], row["role"], row["content"]), ("persona_draft", "assistant", PERSONA), "shown to the admin verbatim")

    def test_a_new_proposal_replaces_the_old_one(self):
        self._call("onboarding_propose_persona", text=PERSONA)
        first = onboarding.draft_card_id(UID)
        self._call("onboarding_propose_persona", text=PERSONA + " Add a joke.")
        self.assertNotEqual(onboarding.draft_card_id(UID), first)
        self.assertTrue(onboarding.get_draft(UID).endswith("Add a joke."))

    def test_pressing_apply_makes_it_her_persona_keeps_the_old_one_and_records_the_step(self):
        persona.save(persona.default_text().replace("assistant, secretary", "assistant and fixer, secretary"))
        old = persona.load_file()
        self._call("onboarding_propose_persona", text=PERSONA)
        self.assertEqual(persona.load_file(), old, "still the old one until the button is pressed")
        h = self._post(ADMIN, "apply")
        self.assertEqual((h.sent[-1]["code"], h.sent[-1]["extra"]["Location"]), (303, "/"))
        live = persona.load_file()
        self.assertIn("dry and warm", live)
        self.assertIn("{{ASSISTANT_NAME}}", live)
        self.assertEqual(live.count("=== PROMPT STARTS ==="), 1)
        self.assertEqual(live.count("=== PROMPT ENDS ==="), 1)
        hist = persona.history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(persona.history_text(hist[0]["name"]).strip(), old.strip(), "the previous persona is snapshotted")
        row = onboarding.state(UID)
        self.assertEqual((row["steps"]["persona"], row["draft_persona"], row["draft_card_id"]), ("applied", None, None))

    def test_the_first_apply_on_the_shipped_default_has_nothing_to_snapshot_and_reset_still_restores_it(self):
        self._call("onboarding_propose_persona", text=PERSONA)
        self._post(ADMIN, "apply")
        self.assertIn("dry and warm", persona.load_file())
        self.assertFalse(persona.history())
        persona.reset_to_default()
        self.assertFalse(persona.is_customised())

    def test_discard_leaves_the_persona_alone(self):
        self._call("onboarding_propose_persona", text=PERSONA)
        self._post(ADMIN, "discard")
        self.assertEqual(persona.load_file(), self.before)
        row = onboarding.state(UID)
        self.assertEqual((row["steps"]["persona"], row["draft_persona"]), ("discarded", None))

    def test_a_member_cannot_press_it_and_nothing_changes(self):
        self._call("onboarding_propose_persona", text=PERSONA)
        h = self._post(MEMBER, "apply")
        self.assertEqual(h.sent[-1]["code"], 403)
        self.assertEqual(persona.load_file(), self.before)
        self.assertEqual(onboarding.get_draft(UID), PERSONA)

    def test_no_draft_means_the_button_does_nothing(self):
        h = self._post(ADMIN, "apply")
        self.assertEqual(h.sent[-1]["code"], 303)
        self.assertEqual(persona.load_file(), self.before)
        self.assertIsNone(onboarding.state(UID)["steps"]["persona"])

    def test_an_apply_that_fails_validation_keeps_the_draft_and_changes_nothing(self):
        self._call("onboarding_propose_persona", text=PERSONA)
        with patch.object(server.persona_admin, "act", return_value=(False, "not saved: too large")):
            h = self._post(ADMIN, "apply")
        self.assertEqual(h.sent[-1]["code"], 400)
        self.assertEqual(onboarding.get_draft(UID), PERSONA)
        self.assertEqual(persona.load_file(), self.before)

    def test_a_restart_drops_a_pending_draft(self):
        self._call("onboarding_propose_persona", text=PERSONA)
        onboarding.request_restart(UID)
        self.assertIsNone(onboarding.get_draft(UID))


class _H(server.Handler):
    def __init__(self):
        self.headers = {}
        self.sent = []

    def send(self, code, body=b"", extra=None, *, ctype="text/html; charset=utf-8"):
        self.sent.append({"code": code, "body": body, "extra": extra or {}})

    def send_json(self, data, code=200):
        self.sent.append({"code": code, "json": data})

    def ip(self):
        return "10.0.0.1"

    def _board_count(self, sess):
        return 0


class Server(unittest.TestCase):
    def setUp(self):
        _reset(UID, MID)

    def _run_turn_prompt(self, user_id):
        run = Mock(return_value={"text": "hi", "usage": USAGE, "hit_round_limit": False, "dispatch_meta": None, "tool_calls": []})
        h = _H()
        sess = ADMIN if user_id == UID else MEMBER
        with patch.object(chat, "run", run):
            h._run_turn(sess, _user(user_id))
        return run.call_args.kwargs.get("extra_message")

    def test_a_chat_turn_carries_the_guide_while_onboarding_is_active(self):
        onboarding.start(UID)
        extra = self._run_turn_prompt(UID)
        self.assertEqual(extra["role"], "system")
        self.assertIn("first-meeting chat", extra["content"])

    def test_a_normal_turn_has_no_extra_message(self):
        conversation.add_message(UID, "user", "hi")
        self.assertIsNone(self._run_turn_prompt(UID))

    def test_begin_has_her_speak_first_and_activates(self):
        h = _H()
        turn = Mock(return_value={"ok": True, "messages": [{"id": 1, "role": "assistant", "content": "Hi, I'm Nori."}]})
        with patch.object(chat, "_resolve_model_chain", return_value=CHAIN), patch.object(h, "_run_turn", turn):
            h.onboarding_begin_post(ADMIN, {})
        out = h.sent[-1]["json"]
        self.assertEqual(out["messages"][0]["content"], "Hi, I'm Nori.")
        self.assertEqual(onboarding.status(UID), "active")
        self.assertIn("Nobody has said anything yet", turn.call_args.kwargs["extra_message"]["content"])

    def test_begin_does_nothing_without_a_model_or_when_not_pending(self):
        h = _H()
        turn = Mock()
        with patch.object(chat, "_resolve_model_chain", return_value=[]), patch.object(h, "_run_turn", turn):
            h.onboarding_begin_post(ADMIN, {})
        self.assertEqual(h.sent[-1]["json"]["messages"], [])
        self.assertEqual(onboarding.status(UID), "pending")
        onboarding.finish(UID)
        with patch.object(chat, "_resolve_model_chain", return_value=CHAIN), patch.object(h, "_run_turn", turn):
            h.onboarding_begin_post(ADMIN, {})
        turn.assert_not_called()

    def test_a_failed_opening_goes_back_to_pending_so_the_next_visit_retries(self):
        h = _H()
        with patch.object(chat, "_resolve_model_chain", return_value=CHAIN), \
                patch.object(h, "_run_turn", Mock(return_value={"ok": False, "error": "couldn't reach the model"})):
            h.onboarding_begin_post(ADMIN, {})
        self.assertEqual(onboarding.status(UID), "pending")

    def _page(self, sess):
        h = _H()
        h.chat_page(dict(sess))
        body = h.sent[-1]["body"]
        return body.decode("utf-8") if isinstance(body, bytes) else body

    def test_chat_page_without_a_model_shows_a_welcome_card_and_does_not_begin(self):
        with patch.object(chat, "_resolve_model_chain", return_value=[]):
            admin, member = self._page(ADMIN), self._page(MEMBER)
        self.assertIn("class=welcome-card", admin)
        self.assertIn("href='/first-run'", admin)
        self.assertIn("class=welcome-card", member)
        self.assertNotIn("/first-run", member.split("welcome-card", 1)[1].split("</div>", 1)[0])
        self.assertIn("household admin", member)
        self.assertIn('"onboarding": ""', admin)

    def test_chat_page_with_a_model_begins_the_introduction(self):
        with patch.object(chat, "_resolve_model_chain", return_value=CHAIN):
            page = self._page(ADMIN)
        self.assertIn('"onboarding": "begin"', page)
        self.assertNotIn("class=welcome-card", page)

    def test_chat_page_for_someone_already_done_never_begins(self):
        conversation.add_message(UID, "user", "hi")
        with patch.object(chat, "_resolve_model_chain", return_value=CHAIN):
            page = self._page(ADMIN)
        self.assertIn('"onboarding": ""', page)

    def test_the_draft_card_has_apply_and_discard_for_the_admin_only_while_pending(self):
        onboarding.start(UID)
        tools.dispatch("onboarding_propose_persona", {"text": PERSONA}, ADMIN)
        first = onboarding.draft_card_id(UID)
        page = self._page(ADMIN)
        self.assertIn("msg-draft", page)
        self.assertIn("value=apply>Apply this persona", page)
        self.assertIn("value=discard", page)
        self.assertIn("action='/onboarding/persona'", page)
        self.assertIn("dry and warm", page)
        tools.dispatch("onboarding_propose_persona", {"text": PERSONA + " More."}, ADMIN)
        page = self._page(ADMIN)
        self.assertEqual(page.count("value=apply>Apply this persona"), 1, "only the newest draft can be applied")
        self.assertIn("no longer pending", page)
        self.assertIn(f"id='message-{first}'", page)
        onboarding.settle_draft(UID, "discarded")
        self.assertNotIn("value=apply>Apply this persona", self._page(ADMIN))

    def test_a_member_is_never_shown_the_apply_button(self):
        onboarding.start(MID)
        conversation.add_message(MID, "assistant", PERSONA, kind="persona_draft", meta={"persona_draft": True})
        self.assertNotIn("value=apply>Apply this persona", self._page(MEMBER))

    def test_the_draft_card_comes_back_in_the_send_response(self):
        onboarding.start(UID)

        def fake_run(session, user_id, name, **kw):
            tools.dispatch("onboarding_propose_persona", {"text": PERSONA}, session)
            return {"text": "I put a draft above.", "usage": USAGE, "hit_round_limit": False, "dispatch_meta": None, "tool_calls": []}

        h = _H()
        with patch.object(chat, "run", fake_run):
            out = h._run_turn(dict(ADMIN), _user(UID))
        kinds = [m["kind"] for m in out["messages"]]
        self.assertIn("persona_draft", kinds)
        self.assertLess(kinds.index("persona_draft"), kinds.index("chat"), "the card sits above her reply")
        card = next(m for m in out["messages"] if m["kind"] == "persona_draft")
        self.assertEqual(card["content"], PERSONA)

    def test_the_script_has_the_begin_hook_and_parses(self):
        import subprocess
        js = server.CHAT_JS
        if js.startswith("<script>"):
            js = js[len("<script>"):-len("</script>")]
        for needle in ("beginOnboarding", "/onboarding/begin", "cfg.onboarding==='begin'", "persona_draft", "/onboarding/persona"):
            self.assertIn(needle, js)
        try:
            ok = subprocess.run(["node", "--version"], capture_output=True).returncode == 0
        except FileNotFoundError:
            ok = False
        if not ok:
            self.skipTest("node isn't available")
        path = os.path.join(os.environ["NORI_DATA_DIR"], "chat_ob.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(js)
        self.assertEqual(subprocess.run(["node", "--check", path], capture_output=True, text=True).returncode, 0)

    def test_restart_route_redirects_to_the_chat_and_resets(self):
        conversation.add_message(UID, "user", "hi")
        onboarding.status(UID)
        h = _H()
        h.do_POST_inner = None
        onboarding.request_restart(UID)             # what the route does
        self.assertEqual(onboarding.status(UID), "pending")
        text = Path(ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn('if path == "/settings/onboarding/restart":', text)
        self.assertIn("meet her again", text)


if __name__ == "__main__":
    unittest.main()

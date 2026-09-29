# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Her own past output is a template (nori/own_output.py): anything malformed that is STORED and later RE-RENDERED into her prompt is imitated.

Fixtures are the real artefacts found in a read-only snapshot of a live Nori database (message ids 100, 484, 3096, 3135 and an image row), not invented ones."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("NORI_DATA_DIR", tempfile.mkdtemp(prefix="nori_own_output_"))
os.environ.setdefault("NORI_PROMPTS_DIR", tempfile.mkdtemp(prefix="nori_own_output_prompts_"))
os.environ.setdefault("NORI_NO_LOGFILE", "1")
sys.path.insert(0, str(ROOT))

import conversation  # noqa: E402
import own_output  # noqa: E402

NAMES = {"set_emotion", "generate_image_selfie", "search_history", "peer1_act", "message_user"}

# The real stored artefacts (content copied from the live database).
PROSE_CALL = 'set_emotion(state="happy")'
LIMIT_LINE = '(hit the 4-step tool-call limit for this turn without finishing -- raise "tool call rounds" in Settings if tasks like this need more steps)'
FAIL_LINE = "(something didn't go through — try asking again)"
COPIED_ANNOTATION = '[you sent him a photo] with: "Midnight Nori, properly introduced. Tender, tasteful, and finally anatomically supervised."'


def row(role, content, kind="chat", meta=None):
    return {"id": 1, "user_id": 1, "ts": 0.0, "role": role, "content": content, "kind": kind, "meta": meta, "emotion": None}


class ScrubTests(unittest.TestCase):
    def test_each_real_artefact_renders_as_nothing(self):
        for c in (PROSE_CALL, LIMIT_LINE, FAIL_LINE, COPIED_ANNOTATION, "(no reply — the model returned nothing)"):
            self.assertEqual(own_output.scrub(c, "assistant", NAMES), "", c)

    def test_an_artefact_inside_real_prose_is_cut_and_the_prose_stays(self):
        out = own_output.scrub("Here you go.\n[calling generate_image_selfie prompt=beach]\nHope you like it.", "assistant", NAMES)
        self.assertIn("Here you go.", out)
        self.assertIn("Hope you like it.", out)
        self.assertNotIn("generate_image_selfie", out)

    def test_ordinary_talk_about_tools_and_photos_is_left_alone(self):
        for c in ("`search_history` returns matching sessions.", "I sent him a photo of the invoice earlier, did it arrive?", "Use search_history if you need it.",
                  "recall (which searches memory) is the right tool"):
            self.assertEqual(own_output.scrub(c, "assistant", NAMES), c, c)

    def test_other_roles_and_non_strings_are_untouched(self):
        self.assertEqual(own_output.scrub(PROSE_CALL, "user", NAMES), PROSE_CALL)
        self.assertEqual(own_output.scrub(None, "assistant", NAMES), None)
        self.assertEqual(own_output.scrub(5, "assistant", NAMES), 5)

    def test_the_record_is_never_edited(self):
        r = row("assistant", COPIED_ANNOTATION)
        conversation.render_for_model(r)
        self.assertEqual(r["content"], COPIED_ANNOTATION)


class RenderTests(unittest.TestCase):
    """conversation.render_for_model is the one choke point the window, compaction and the composition breakdown all read through."""

    def setUp(self):
        import tools
        self._reg = dict(tools._REGISTRY)
        for n in NAMES:
            tools._REGISTRY.setdefault(n, object())
        self.addCleanup(lambda: (tools._REGISTRY.clear(), tools._REGISTRY.update(self._reg)))

    def test_stored_artefacts_never_reach_the_model_verbatim(self):
        for c in (PROSE_CALL, LIMIT_LINE, FAIL_LINE, COPIED_ANNOTATION):
            self.assertEqual(conversation.render_for_model(row("assistant", c)), "", c)

    def test_a_photo_she_sent_renders_as_a_marked_app_note_not_a_bracket_she_can_copy(self):
        t = conversation.render_for_model(row("assistant", "There you are.", kind="image"))
        self.assertTrue(t.startswith("(app note, not part of what you said:"), t)
        self.assertNotIn("[", t)
        self.assertNotIn(" him ", t)                                   # nothing assumes who the user is
        self.assertIn("There you are.", t)

    def test_a_captions_own_artefact_is_scrubbed_too(self):
        t = conversation.render_for_model(row("assistant", COPIED_ANNOTATION, kind="image"))
        self.assertNotIn("[you sent", t)

    def test_the_users_photo_annotation_assumes_nothing_about_them(self):
        t = conversation.render_for_model(row("user", "look", kind="image", meta='{"description": "a red door"}'))
        self.assertIn("a red door", t)
        self.assertNotRegex(t, r"\b[Hh]e\b")

    def test_the_users_own_words_are_never_scrubbed(self):
        self.assertEqual(conversation.render_for_model(row("user", PROSE_CALL)), PROSE_CALL)

    def test_the_app_note_about_why_she_said_something_survives(self):
        t = conversation.render_for_model(row("assistant", "Quick check-in.", kind="proactive", meta='{"reason": "he asked me to nudge him at 3"}'))
        self.assertIn("Quick check-in.", t)
        self.assertIn("he asked me to nudge him at 3", t)

    def test_an_artefact_only_proactive_message_renders_empty_with_no_dangling_note(self):
        self.assertEqual(conversation.render_for_model(row("assistant", PROSE_CALL, kind="proactive", meta='{"reason": "x"}')), "")


class SurfaceTriageTests(unittest.TestCase):
    """Every module that reads stored messages must be triaged: does it show HER words to a model? (then it scrubs), or not (then it says why)."""
    N = ROOT
    READS = re.compile(r"FROM messages|conversation\.(?:recent|since|before|in_range|search|history_page|get_own|latest_user_message_after)\(")
    TRIAGED = {
        "conversation.py": ("scrubbed", "render_for_model (window, compaction input) and search() results"),
        "context.py": ("scrubbed", "builds the window through conversation.render_for_model, and skips lines that render empty"),
        "compaction.py": ("scrubbed", "summarises through conversation.render_for_model"),
        "memory.py": ("scrubbed", "the reflection prompt's conversation"),
        "peers.py": ("scrubbed", "the recent-exchanges block: what she sent to a peer"),
        "server.py": ("no prompt", "the chat and history pages are the operator's own view, not a prompt"),
        "turns.py": ("no prompt", "counts and reads the user's unanswered messages"),
        "diagnostics.py": ("no prompt", "kind='tool' meta for the diagnostics page"),
    }

    def readers(self):
        return {p.name: p.read_text(encoding="utf-8", errors="ignore") for p in sorted(self.N.glob("*.py")) if p.name != "own_output.py" and self.READS.search(p.read_text(encoding="utf-8", errors="ignore"))}

    def test_every_reader_is_triaged(self):
        untriaged = sorted(set(self.readers()) - set(self.TRIAGED))
        self.assertEqual(untriaged, [], "these modules read stored messages but are not triaged in this file: decide whether they show HER words to a model (then scrub via "
                                        "own_output) and list them with the reason -- see nori/own_output.py")

    def test_a_scrubbed_module_really_uses_own_output(self):
        for mod, (kind, why) in self.TRIAGED.items():
            if kind == "scrubbed":
                src = (self.N / mod).read_text(encoding="utf-8")
                self.assertTrue("own_output" in src or "render_for_model" in src, f"{mod} is triaged as scrubbed but never scrubs ({why})")

    def test_no_stale_entries(self):
        self.assertEqual(sorted(m for m in self.TRIAGED if not (self.N / m).exists()), [])

    def test_the_generation_time_detector_and_the_render_time_scrubber_share_one_pattern(self):
        chat_src = (self.N / "chat.py").read_text(encoding="utf-8")
        self.assertIn("_leak_pattern = own_output.leak_pattern", chat_src)
        self.assertEqual(len(re.findall(r"def (?:_?leak_pattern)\(", chat_src)), 0, "chat.py must not carry its own copy of the pattern")


class RealSurfaceTests(unittest.TestCase):
    """Each place that shows her her own past words, exercised against a real database: none of the stored artefacts may come out the other side."""

    @classmethod
    def setUpClass(cls):
        import accounts
        import store
        store.init()
        cls.user = accounts.bootstrap_admin("Tester", "testpass123")
        cls.uid = cls.user["id"]
        import tools
        cls._reg = dict(tools._REGISTRY)
        for n in NAMES:
            tools._REGISTRY.setdefault(n, object())
        for role, c, kind in (("user", "hello", "chat"), ("assistant", "Morning! Midnight plans sorted.", "chat"), ("user", "send a photo", "chat"),
                              ("assistant", "Sure thing." + chr(10) + "[calling generate_image_selfie prompt=beach]" + chr(10) + "Done, enjoy.", "chat"), ("assistant", COPIED_ANNOTATION, "chat"), ("assistant", PROSE_CALL, "chat"), ("assistant", LIMIT_LINE, "chat"),
                              ("user", "thanks, what next?", "chat"), ("assistant", "Next up: the dentist at 3.", "chat")):
            conversation.add_message(cls.uid, role, c, kind=kind)
        conversation.add_message(cls.uid, "assistant", "Here you go.", kind="image")

    @classmethod
    def tearDownClass(cls):
        import tools
        tools._REGISTRY.clear()
        tools._REGISTRY.update(cls._reg)

    ARTEFACTS = ("[you sent", "set_emotion(", "tool-call limit", "[calling generate")

    def assertClean(self, text, where):
        for a in self.ARTEFACTS:
            self.assertNotIn(a, text, f"{where} still shows her a stored artefact: {a!r}")

    def test_the_window_she_is_sent(self):
        import context
        msgs = context.build_messages(self.uid, "Tester")
        joined = chr(10).join(m["content"] for m in msgs if m["role"] != "system")
        self.assertClean(joined, "the conversation window")
        self.assertIn("Next up: the dentist at 3.", joined)
        self.assertIn("send a photo", joined)                                          # the user's own words are never scrubbed
        self.assertTrue(all(m["content"] for m in msgs), "an empty message was sent to the model")
        self.assertIn("(app note, not part of what you said:", joined)                 # the photo she sent is described by the app, marked as such

    def test_the_composition_measurement_counts_the_same_window(self):
        import context
        comp = context.composition_breakdown({"user_id": self.uid, "role": "admin", "workspace_id": self.user["workspace_id"]}, self.uid, "Tester")
        self.assertGreater(comp["total_tokens"], 0)

    def test_history_search_results(self):
        r = conversation.search(self.uid, hours_ago=24)
        text = repr(r)
        self.assertClean(text, "search_history")
        self.assertIn("Done, enjoy.", text)                                            # what she really said around an artefact is kept
        self.assertTrue(all(m["content"] for seg in r["segments"] for m in seg["messages"]), "an empty message was returned")

    def test_the_memory_reflection_prompt(self):
        import chat
        import memory
        seen = {}

        def fake(msgs, **kw):
            seen["t"] = msgs[-1]["content"]
            return {"content": "{}", "usage": {}}
        real = chat.call
        chat.call = fake
        try:
            memory.reflect(self.uid)
        finally:
            chat.call = real
        self.assertIn("dentist", seen.get("t", ""))
        self.assertClean(seen["t"], "the memory reflection prompt")

    def test_the_peer_exchange_block_shows_what_she_sent_scrubbed(self):
        import json
        import peers
        import store
        import uuid
        res = peers.create_peer({"user_id": self.uid, "workspace_id": self.user["workspace_id"], "role": "admin"}, scope="user", name="peer-x",
                                url="https://example.invalid/paci/inbound/x", psk=os.urandom(12).hex())
        pid = res["peer_id"]
        for text in (PROSE_CALL, "Sure, I will check the calendar and get back to you."):
            store.write(lambda c, text=text: c.execute(
                "INSERT INTO peer_messages(peer_id, conversation_id, message_id, direction, type, seq, body_json, ts, status) VALUES (?,?,?,?,?,?,?,?,?)",
                (pid, str(uuid.uuid4()), str(uuid.uuid4()), "sent", "message", 1, json.dumps({"text": text}), __import__("time").time(), "pending")))
        block = peers.recent_context_block(self.uid)
        self.assertIn("check the calendar", block)
        self.assertClean(block, "the recent peer exchanges block")
        self.assertNotIn("{'text'", block, "what she sent must read as her words, not as a Python dict")


if __name__ == "__main__":
    unittest.main()

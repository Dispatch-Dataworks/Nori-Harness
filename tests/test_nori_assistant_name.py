# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The configurable assistant name (2026-09-25, the operator: "the product
is called Nori (Nori-Harness) we just allow people to name and assign
their own identity to their own instance"). Two things this proves, not
just one: (1) an operator who changes nothing gets exactly what shipped
before this feature existed -- the default-path byte-identical
requirement -- and (2) a rename actually reaches every render-time
surface, including the one specifically flagged: PACI's outbound
agent_name, which used to be frozen at peer-creation time and would have
kept advertising the old name forever after a rename."""
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_PROMPTS_DIR = Path(tempfile.mkdtemp(prefix="nori_test_assistant_name_prompts_"))
shutil.copy(ROOT / "prompts" / "persona.default.md", _PROMPTS_DIR / "persona.default.md")
os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_assistant_name_")
os.environ["NORI_PROMPTS_DIR"] = str(_PROMPTS_DIR)
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import config  # noqa: E402
import context  # noqa: E402
import peers  # noqa: E402
import persona  # noqa: E402
import store  # noqa: E402

store.init()


class AssistantNameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.user = accounts.bootstrap_admin("Tester", "testpass123")
        cls.wsid = cls.user["workspace_id"]

    def setUp(self):
        store.write(lambda c: c.execute(
            "DELETE FROM settings WHERE scope='workspace' AND scope_id=? AND key='assistant_name'", (self.wsid,)))

    # ── the default path must be untouched by this feature existing ────────
    def test_default_is_exactly_what_shipped_before_this_feature(self):
        self.assertEqual(config.get("workspace", self.wsid, "assistant_name"), "Nori")
        prompt = context.build_system(self.user["id"], "Tester")
        self.assertIn("Your name is Nori.", prompt)
        self.assertIn("You are Nori — an assistant, secretary, and life coach.", prompt)

    def test_load_prompt_with_no_user_id_also_defaults_to_nori(self):
        """Every existing call site that never passed a user_id (tests,
        an admin preview) must keep behaving exactly as before."""
        self.assertIn("You are Nori —", persona.load_prompt())

    # ── a rename actually reaches the model-facing render ───────────────────
    def test_a_rename_reaches_the_name_time_header_and_the_persona(self):
        config.set("workspace", self.wsid, "assistant_name", "Aria")
        prompt = context.build_system(self.user["id"], "Tester")
        self.assertIn("Your name is Aria.", prompt)
        self.assertIn("You are Aria — an assistant, secretary, and life coach.", prompt)
        self.assertNotIn("Nori", prompt)

    def test_an_operators_own_custom_persona_can_use_the_placeholder_too(self):
        import promptdoc
        doc = promptdoc.PromptDoc("persona")
        ok, msg = doc.save(f"{promptdoc.START}\nCall me {{{{ASSISTANT_NAME}}}} today.\n{promptdoc.END}\n")
        self.assertTrue(ok, msg)
        try:
            config.set("workspace", self.wsid, "assistant_name", "Juno")
            self.assertEqual(persona.load_prompt(self.user["id"]), "Call me Juno today.")
        finally:
            doc.path.unlink(missing_ok=True)

    # ── never baked into a file -- the single-source-at-render-time rule ───
    def test_reset_to_default_writes_the_literal_placeholder_never_a_baked_name(self):
        config.set("workspace", self.wsid, "assistant_name", "Aria")
        ok, _ = persona.reset_to_default()
        self.assertTrue(ok)
        on_disk = persona.load_file()
        self.assertIn("{{ASSISTANT_NAME}}", on_disk, "reset must write the template, never a find-and-replaced name")
        self.assertNotIn("You are Aria", on_disk)

    def test_default_text_and_is_default_are_never_substituted(self):
        """default_text()/is_default() operate on the raw file (what a
        reset WRITES) -- substitution belongs only to load_prompt(), the
        one real model-facing render. Mixing the two would either break
        'is this the shipped default' for every non-default name, or
        silently bake a name into the file on reset."""
        config.set("workspace", self.wsid, "assistant_name", "Aria")
        self.assertIn("{{ASSISTANT_NAME}}", persona.default_text())
        self.assertNotIn("Aria", persona.default_text())

    # ── PACI: the finding the operator asked to be protected ────────────────
    def test_paci_outbound_agent_name_is_live_not_frozen_at_connect_time(self):
        """The exact trap: a peer created while the instance was still
        called 'Nori' must NOT keep advertising 'Nori' after a rename --
        self_agent_name is a frozen column, unused for outbound identity
        now, on purpose."""
        peer = {"scope": "workspace", "scope_id": self.wsid, "self_agent_name": "Nori"}
        self.assertEqual(peers._peer_assistant_name(peer), "Nori")
        config.set("workspace", self.wsid, "assistant_name", "Aria")
        self.assertEqual(peers._peer_assistant_name(peer), "Aria",
                        "outbound PACI identity must track a rename even on an already-established peer")

    def test_paci_outbound_agent_name_resolves_a_user_scoped_peer_too(self):
        config.set("workspace", self.wsid, "assistant_name", "Aria")
        peer = {"scope": "user", "scope_id": self.user["id"], "self_agent_name": "Nori"}
        self.assertEqual(peers._peer_assistant_name(peer), "Aria")

    def test_hello_and_hello_ack_bodies_both_carry_the_live_name(self):
        config.set("workspace", self.wsid, "assistant_name", "Aria")
        peer = {"scope": "workspace", "scope_id": self.wsid, "self_agent_id": "nori:abc12345",
               "self_agent_name": "Nori", "turn_limit": 10, "cooldown_minutes": 60, "daily_cap": 4,
               "trust_level": "none", "url": "https://example.invalid/paci", "enabled": 1}
        ack = peers._hello_ack(peer)
        self.assertEqual(ack["agent_name"], "Aria")
        self.assertEqual(ack["agent_id"], "nori:abc12345")   # agent_id is untouched -- identity/auth stays on that field, per the PACI specification

    # ── server.py's UI surfaces read the same live config, not a copy ──────
    def test_server_no_longer_hardcodes_the_name_at_the_surfaces_this_feature_touches(self):
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"aria-label='View Nori avatar'")
        self.assertNotRegex(src, r"<div class=hdr-name>Nori</div>")
        self.assertNotRegex(src, r"aria-label='Talk to Nori'")
        self.assertNotRegex(src, r'"name":\s*"Nori",\s*"short_name":\s*"Nori"')
        self.assertNotRegex(src, r"<h2>Nori's current model</h2>")
        self.assertIn('assistant_name = config.get("workspace", sess["workspace_id"], "assistant_name")', src)

    def test_the_admin_form_to_actually_change_it_exists_on_the_avatars_tab(self):
        """A setting nothing lets an operator reach is the reachability
        bug this project has hit before (CLAUDE.md) -- the control has to
        actually be on a page, postable, and wired to config.set()."""
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertRegex(src, r"name=assistant_name maxlength=60 required")
        self.assertRegex(src, r"action='/admin/avatars'")
        self.assertIn('config.set("workspace", wsid, key, val)', src)
        self.assertIn('for key in ("assistant_name", "emotion_enabled"):', src)

    def test_paci_software_identity_stays_nori_deliberately(self):
        """The other half of the split: harness/software identification is
        NOT this setting and must never read it."""
        src = (ROOT / "peers.py").read_text(encoding="utf-8")
        self.assertIn('"User-Agent": "Nori-PACI/1.0"', src)


if __name__ == "__main__":
    unittest.main()

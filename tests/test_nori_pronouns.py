# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Pronouns (2026-10-28): the setting existed but could only be changed in the first-run wizard, and the pages themselves said "she/her" and "him" whatever it
was set to (found on Iron, a robot).

Pinned: the pronoun choice is in Settings next to the name (and "it/its" is one); it reaches the prompt for every choice; the page copy says the assistant's
NAME instead of a pronoun (one token, filled in when the page is sent, escaped), so it reads right for any pronouns; nothing in the app's own page copy,
the guide she is given or the persona template hard-codes she/her/hers/herself (a scan of the string literals, so it fails when someone writes
"she can..." into a page again); and the creator labels assume nothing about the person."""
import io
import os
import shutil
import sys
import tempfile
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_pronouns_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_pronouns_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"
for _f in ("persona.default.md", "persona.template.md"):
    shutil.copy(ROOT / "prompts" / _f, os.environ["NORI_PROMPTS_DIR"])

import accounts  # noqa: E402
import config  # noqa: E402
import context  # noqa: E402
import onboarding  # noqa: E402
import persona  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Ben", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
ADMIN = {"user_id": UID, "workspace_id": WS, "role": "admin", "csrf": "csrf-tok"}
TOKEN = "⟦n⟧"


class _H(server.Handler):
    """The real send() is kept (that is where the name goes in); only the socket is faked."""

    def __init__(self):
        self.headers = {}
        self.wfile = type("W", (), {"write": lambda s, b: self.written.append(b)})()
        self.written = []
        self.command = "GET"
        self.raw = []

    def send_response(self, code, message=None):
        pass

    def send_header(self, k, v):
        pass

    def end_headers(self):
        pass


def sent(body, ctype="text/html; charset=utf-8"):
    h = _H()
    h.send(200, body, ctype=ctype)
    return b"".join(h.written).decode("utf-8")


class Setting(unittest.TestCase):
    def setUp(self):
        config.set("workspace", WS, "assistant_pronouns", "she/her")
        config.set("workspace", WS, "assistant_name", "Iron")

    def test_it_its_is_a_choice(self):
        self.assertIn("it/its", config.PRONOUNS)
        config.set("workspace", WS, "assistant_pronouns", "it/its")
        self.assertEqual(config.get("workspace", WS, "assistant_pronouns"), "it/its")
        with self.assertRaises(ValueError):
            config.set("workspace", WS, "assistant_pronouns", "xe/xem")

    def test_every_choice_reaches_the_prompt(self):
        for p in config.PRONOUNS:
            config.set("workspace", WS, "assistant_pronouns", p)
            self.assertIn(f"Your pronouns are {p}", persona.pronoun_sentence_for_user(UID))

    def _avatars(self):
        h = _H()
        h.send = lambda code, body=b"", extra=None, **kw: h.raw.append(body)
        h._settings_response = lambda sess, tab, body, **kw: h.raw.append(body)
        return h

    def test_settings_has_the_pronoun_choice_next_to_the_name(self):
        h = self._avatars()
        h.avatars_admin_page(dict(ADMIN))
        html = h.raw[-1]
        self.assertIn("name and pronouns", html)
        self.assertIn("name=assistant_name", html)
        for p in config.PRONOUNS:
            self.assertIn(f"name=assistant_pronouns value='{p}'", html)
        self.assertIn("value='she/her' checked", html)
        config.set("workspace", WS, "assistant_pronouns", "it/its")
        h = self._avatars()
        h.avatars_admin_page(dict(ADMIN))
        self.assertIn("value='it/its' checked", h.raw[-1])
        self.assertNotIn("value='she/her' checked", h.raw[-1])

    def test_saving_the_settings_form_changes_the_pronouns(self):
        h = self._avatars()
        h.avatars_admin_post(dict(ADMIN), {"assistant_name": "Iron", "assistant_pronouns": "they/them"})
        self.assertEqual(config.get("workspace", WS, "assistant_pronouns"), "they/them")
        h = self._avatars()
        h.avatars_admin_post(dict(ADMIN), {"assistant_pronouns": "nonsense"})
        self.assertEqual(config.get("workspace", WS, "assistant_pronouns"), "they/them", "a bad value changes nothing")
        self.assertIn("assistant_pronouns must be one of", h.raw[-1])


class NameInPages(unittest.TestCase):
    def setUp(self):
        config.set("workspace", WS, "assistant_name", "Iron")

    def test_the_token_becomes_the_current_name_when_a_page_is_sent(self):
        out = sent(f"<p>{TOKEN} can read tabs. Hidden from {TOKEN}. {TOKEN}'s persona.</p>".encode())
        self.assertEqual(out, "<p>Iron can read tabs. Hidden from Iron. Iron's persona.</p>")
        config.set("workspace", WS, "assistant_name", "Robo")
        self.assertIn("Robo can read", sent(f"{TOKEN} can read".encode()))

    def test_the_name_is_escaped(self):
        config.set("workspace", WS, "assistant_name", "R&D <b>\"x\"")
        out = sent(f"<p title='{TOKEN}'>{TOKEN}</p>".encode())
        self.assertNotIn("<b>", out)
        self.assertIn("R&amp;D &lt;b&gt;", out)

    def test_other_responses_are_left_alone(self):
        self.assertEqual(sent(f'{{"a":"{TOKEN}"}}'.encode(), ctype="application/json"), f'{{"a":"{TOKEN}"}}')

    def test_a_real_page_has_no_token_left(self):
        h = _H()
        h.send = lambda code, body=b"", extra=None, **kw: h.raw.append(body)
        h._settings_response = lambda sess, tab, body, **kw: h.raw.append(body)
        h.avatars_admin_page(dict(ADMIN))
        self.assertIn(TOKEN, h.raw[-1], "the page is written with the token")
        self.assertNotIn(TOKEN, sent(h.raw[-1].encode()))
        self.assertIn("Iron", sent(h.raw[-1].encode()))

    def test_the_guide_she_is_given_has_the_name_and_no_token(self):
        user = accounts.get_user(UID)
        guide = onboarding.guide(user)
        self.assertNotIn(TOKEN, guide)
        self.assertIn("Iron's persona", guide)
        for stop in onboarding._stops(user):
            self.assertNotIn(TOKEN, stop["title"] + stop["what"])
        self.assertIn("Iron", " ".join(s["what"] for s in onboarding._stops(user)))

    def test_the_creator_labels_assume_nothing_about_the_person(self):
        h = _H()
        label = h._task_creator_label if hasattr(h, "_task_creator_label") else None
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertNotIn('"user": "him"', src)
        self.assertNotIn('("user", "him")', src)
        self.assertNotIn('"nori": "her"', src)


GENDERED = __import__("re").compile(r"\b(she|her|hers|herself|she's)\b", __import__("re").I)
# An example of how to describe a person (placeholder text in the avatar builder's description box), not a statement about the assistant.
ALLOWED = ("A woman in her early thirties",)


def string_literals(path: Path):
    """(line, text) of every string literal that is not a docstring (those and comments are for developers, not for pages or prompts)."""
    src = path.read_text(encoding="utf-8")
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    prev = None
    for i, t in enumerate(toks):
        if t.type == tokenize.STRING:
            nxt = next((x for x in toks[i + 1:] if x.type not in (tokenize.NL, tokenize.COMMENT)), None)
            docstring = (prev is None or prev.type in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)) and nxt is not None and nxt.type == tokenize.NEWLINE
            if not docstring:
                yield t.start[0], t.string
        if t.type not in (tokenize.NL, tokenize.COMMENT):
            prev = t


class NoHardCodedPronouns(unittest.TestCase):
    def scan(self, name):
        bad = []
        for line, text in string_literals(ROOT / name):
            if any(a in text for a in ALLOWED):
                continue
            for m in GENDERED.finditer(text):
                bad.append(f"{name}:{line}: {text[max(0, m.start() - 40):m.end() + 40]!r}")
        return bad

    def test_page_copy_does_not_hard_code_she_or_her(self):
        bad = self.scan("server.py")
        self.assertEqual(bad, [], "write the assistant's name (the token) or 'the assistant', not a pronoun:\n" + "\n".join(bad[:15]))

    def test_the_guide_firstrun_and_builder_text_do_not_either(self):
        bad = [b for f in ("onboarding.py", "firstrun.py", "avatar_builder.py") for b in self.scan(f)]
        self.assertEqual(bad, [], "\n".join(bad[:15]))

    def test_the_prompt_templates_do_not_either(self):
        for f in ("persona.template.md", "tool-usage.template.md"):
            text = (ROOT / "prompts" / f).read_text(encoding="utf-8")
            self.assertEqual(GENDERED.findall(text), [], f)

    def test_the_scan_itself_catches_a_pronoun(self):
        tmp = Path(tempfile.mkdtemp()) / "x.py"
        tmp.write_text('"""she is fine in a docstring"""\nx = "she can read"\n# her too\ny = "the assistant can read"\n', encoding="utf-8")
        found = list(string_literals(tmp))
        self.assertEqual([t for _, t in found if GENDERED.search(t)], ['"she can read"'])


if __name__ == "__main__":
    unittest.main()

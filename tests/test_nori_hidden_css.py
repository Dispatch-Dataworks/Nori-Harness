# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Elements toggled with the `hidden` attribute must not carry a bare `display:`
rule (2026-10-08). Author CSS beats the browser's built-in [hidden]{display:none},
so `.board-add-menu{display:flex}` left the board's three add options permanently
open over everything, and `.badge{display:flex}` kept an empty board's "0" badge
on screen. The fix is the pattern the stylesheet already uses elsewhere: display
only in a `:not([hidden])` rule.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "server.py"), encoding="utf-8") as f:
    SRC = f.read()

# Selectors the app hides by setting/removing the `hidden` attribute.
HIDDEN_TOGGLED = (".board-add-menu", ".badge")


def _rules(selector: str):
    """Every CSS block whose selector list contains exactly `selector` (optionally
    with a :not([hidden]) qualifier), as (selector_text, body)."""
    out = []
    for m in re.finditer(r"(?m)^(" + re.escape(selector) + r"(?::not\(\[hidden\]\))?)\s*\{([^}]*)\}", SRC):
        out.append((m.group(1), m.group(2)))
    return out


class HiddenToggledElementsCanActuallyHide(unittest.TestCase):
    def test_no_bare_display_rule(self):
        for sel in HIDDEN_TOGGLED:
            rules = _rules(sel)
            self.assertTrue(rules, f"no CSS rule found for {sel}")
            for text, body in rules:
                if ":not([hidden])" in text:
                    continue
                self.assertNotRegex(body, r"display\s*:", f"{sel}: a bare display rule defeats the hidden attribute")

    def test_display_is_declared_only_when_not_hidden(self):
        for sel in HIDDEN_TOGGLED:
            guarded = [b for t, b in _rules(sel) if ":not([hidden])" in t]
            self.assertEqual(len(guarded), 1, sel)
            self.assertIn("display:flex", guarded[0].replace(" ", ""), sel)

    def test_the_add_menu_starts_hidden_and_is_toggled_through_the_attribute(self):
        self.assertIn("id=boardAddMenu hidden>", SRC)
        self.assertIn("var open=!bam.hidden;", SRC)
        self.assertIn("bam.hidden=open;", SRC)


if __name__ == "__main__":
    unittest.main()

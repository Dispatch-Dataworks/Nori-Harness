# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""self_knowledge.py's own explain_self tool -- focused on the 2026-09-19
'identity' topic addition (real public URL, real page list), the direct
fix for two confabulation instances: she couldn't answer "what's your
address" at all, and separately invented a settings-page "about" that
didn't exist yet. Other topics (tools/memory/compaction/paci/sub_agents)
already have real coverage via the modules they read from; this file
only covers what's new."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_self_knowledge_")
os.environ["NORI_NO_LOGFILE"] = "1"

import self_knowledge  # noqa: E402


def _clear_env(*keys):
    for k in keys:
        os.environ.pop(k, None)


class IdentityTopicTests(unittest.TestCase):
    def test_listed_in_overview(self):
        overview = self_knowledge.explain_self_impl({}, topic=None)
        self.assertIn("identity", overview)

    def test_accepted_as_a_valid_topic(self):
        self.assertIn("identity", self_knowledge._TOPICS)

    def test_public_url_read_when_configured(self):
        with patch.dict(os.environ, {"NORI_PUBLIC_URL": "https://a.example.com/"}):
            r = self_knowledge.explain_self_impl({}, topic="identity")
        # Trailing slash stripped, matching server.py's own PUBLIC_URL
        # normalization -- the two must never silently disagree about
        # what her "real" address actually is.
        self.assertEqual(r["public_url"], "https://a.example.com")

    def test_public_url_is_none_not_a_guess_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            _clear_env("NORI_PUBLIC_URL")
            r = self_knowledge.explain_self_impl({}, topic="identity")
        self.assertIsNone(r["public_url"])

    def test_pages_list_includes_the_two_real_gaps_that_motivated_this(self):
        r = self_knowledge.explain_self_impl({}, topic="identity")
        # /about: she previously didn't reliably know this real, public
        # page existed. /settings: her own invented "about" lives inside
        # this real one now, not as a page of its own -- the list should
        # say so, not just name the path.
        self.assertIn("/about", r["pages"])
        self.assertIn("/settings", r["pages"])
        self.assertIn("about", r["pages"]["/settings"].lower())

    def test_pages_list_never_invents_a_standalone_settings_about_route(self):
        r = self_knowledge.explain_self_impl({}, topic="identity")
        self.assertNotIn("/settings/about", r["pages"])
        self.assertNotIn("/about/settings", r["pages"])

    def test_unknown_topic_still_names_identity_as_a_valid_choice(self):
        r = self_knowledge.explain_self_impl({}, topic="bogus")
        self.assertIn("identity", r["error"])


if __name__ == "__main__":
    unittest.main()

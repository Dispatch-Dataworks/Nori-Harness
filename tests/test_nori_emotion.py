# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The emotion off switch (2026-09-25, the operator: "I appreciate it but
I can see how others would find it annoying") -- both states, checked at the one
real choke point (emotion.get_state()) and at the two other surfaces that
would otherwise leak the state while off: the precheck reminder line and
the set_emotion tool itself."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_emotion_")
os.environ["NORI_NO_LOGFILE"] = "1"

import accounts  # noqa: E402
import config  # noqa: E402
import emotion  # noqa: E402
import precheck  # noqa: E402
import store  # noqa: E402
import tools  # noqa: E402

store.init()


class EmotionToggleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.user = accounts.bootstrap_admin("Tester", "testpass123")

    def setUp(self):
        self.user = self.__class__.user
        self.wsid = self.user["workspace_id"]
        self.session = dict(role="admin", user_id=self.user["id"], workspace_id=self.wsid, csrf="t")
        store.write(lambda c: c.execute("DELETE FROM emotion_state WHERE user_id=?", (self.user["id"],)))
        store.write(lambda c: c.execute(
            "DELETE FROM settings WHERE scope='workspace' AND scope_id=? AND key='emotion_enabled'", (self.wsid,)))

    def test_enabled_by_default(self):
        self.assertTrue(emotion.is_enabled(self.wsid))

    # ── get_state() ──────────────────────────────────────────────────────
    def test_enabled_reports_a_real_set_state(self):
        emotion._set_state(self.user["id"], "curious")
        self.assertEqual(emotion.get_state(self.user["id"]), "curious")

    def test_disabled_always_reports_neutral_even_with_a_real_state_set(self):
        emotion._set_state(self.user["id"], "curious")
        config.set("workspace", self.wsid, "emotion_enabled", False)
        self.assertEqual(emotion.get_state(self.user["id"]), emotion.DEFAULT_STATE)

    def test_re_enabling_reveals_the_state_that_was_there_all_along(self):
        emotion._set_state(self.user["id"], "curious")
        config.set("workspace", self.wsid, "emotion_enabled", False)
        config.set("workspace", self.wsid, "emotion_enabled", True)
        self.assertEqual(emotion.get_state(self.user["id"]), "curious")

    # ── precheck line ────────────────────────────────────────────────────
    def test_precheck_line_present_when_enabled(self):
        line = emotion._precheck_line(self.session, self.user["id"])
        self.assertIsNotNone(line)
        self.assertIn("visible state", line)

    def test_precheck_line_absent_when_disabled(self):
        config.set("workspace", self.wsid, "emotion_enabled", False)
        self.assertIsNone(emotion._precheck_line(self.session, self.user["id"]))

    def test_precheck_build_block_has_no_gap_when_disabled(self):
        """precheck.build_block() must not add an empty bullet or a stray
        block for a check that has nothing to say -- the slot just closes
        up (see precheck.py's own build_block() docstring)."""
        precheck._CHECKS[:] = [fn for fn in precheck._CHECKS if fn is emotion._precheck_line]
        config.set("workspace", self.wsid, "emotion_enabled", True)
        self.assertIsNotNone(precheck.build_block(self.session, self.user["id"]))
        config.set("workspace", self.wsid, "emotion_enabled", False)
        self.assertIsNone(precheck.build_block(self.session, self.user["id"]))

    # ── the tool itself ──────────────────────────────────────────────────
    def test_set_emotion_is_offered_when_enabled(self):
        self.assertIn("set_emotion", {s["function"]["name"] for s in tools.active_schemas(self.session)})

    def test_set_emotion_is_not_offered_when_disabled(self):
        config.set("workspace", self.wsid, "emotion_enabled", False)
        self.assertNotIn("set_emotion", {s["function"]["name"] for s in tools.active_schemas(self.session)})

    def test_set_emotion_dispatch_refuses_when_disabled(self):
        config.set("workspace", self.wsid, "emotion_enabled", False)
        result = tools.dispatch("set_emotion", {"state": "curious"}, self.session)
        self.assertIn("error", result)
        self.assertEqual(emotion.get_state(self.user["id"]), emotion.DEFAULT_STATE)

    def test_set_emotion_dispatch_works_when_enabled(self):
        result = tools.dispatch("set_emotion", {"state": "curious"}, self.session)
        self.assertEqual(result.get("ok"), True)
        self.assertEqual(emotion.get_state(self.user["id"]), "curious")


if __name__ == "__main__":
    unittest.main()

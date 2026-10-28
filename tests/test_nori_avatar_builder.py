# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Avatar builder (2026-10-28, avatar_builder.py): describe a neutral face, get the whole set of expressions.
Real SQLite, real files in scratch directories, the real page renderers; only the image call (chat.openrouter_image) is faked.

Pinned: pictures need a connected OpenRouter key (the connected provider's, not only the environment's) and an image model; the description is checked and
every request carries the adult/clothed text; three candidates, one is chosen and becomes the reference every other portrait is drawn from (passed as the
input reference, with that state's look in the prompt), a few at a time in the background; a failed or interrupted picture is retried without redrawing the
rest; nothing is installed until all are drawn; install moves the old avatars to a backup (and leaves no old-format file shadowing a new one), replaces the
selfie reference, and optionally the selfie text; restore undoes all of it; the builder's spend is logged but kept out of today's household cap; the pages
are admin-only and draft files cannot be reached by path tricks."""
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_avbuild_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_avbuild_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"
os.environ.pop("OPENROUTER_API_KEY", None)
for _f in ("persona.default.md", "persona.template.md"):
    shutil.copy(ROOT / "prompts" / _f, os.environ["NORI_PROMPTS_DIR"])

import accounts  # noqa: E402
import avatar_builder as ab  # noqa: E402
import chat  # noqa: E402
import config  # noqa: E402
import emotion  # noqa: E402
import imagegen  # noqa: E402
import medialog  # noqa: E402
import providers  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Ben", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
_inv_id, _inv_tok = accounts.create_invite(WS, "Gigi", "member", UID)
MID = accounts.accept_invite(_inv_tok, "memberpass123")["id"]
ADMIN = {"user_id": UID, "workspace_id": WS, "role": "admin", "csrf": "csrf-tok"}
MEMBER = {"user_id": MID, "workspace_id": WS, "role": "member", "csrf": "csrf-mem"}

DESC = "A woman in her early thirties with short silver hair, green eyes and light olive skin, a charcoal cardigan over a white shirt."
OTHERS = [s for s in emotion.STATES if s != "neutral"]


def png(color=(120, 80, 60), fmt="PNG") -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 80), color).save(buf, fmt)
    return buf.getvalue()


class FakeImages:
    """Stands in for chat.openrouter_image: records every call, can fail chosen prompts, hold calls open, and report concurrency."""

    def __init__(self):
        self.calls = []
        self.fail_if = lambda prompt: False
        self.gate = None
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.n = 0
        self.bytes_for = None

    def __call__(self, prompt, *, model, seed=None, n=1, references=None):
        with self.lock:
            self.n += 1
            k = self.n
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append({"prompt": prompt, "model": model, "references": references})
        try:
            if self.gate is not None:
                self.gate.wait(timeout=10)
            else:
                time.sleep(0.02)
            if self.fail_if(prompt):
                return {"ok": False, "reason": "image API 500: boom"}
            data = self.bytes_for(prompt) if self.bytes_for else png((k * 7 % 255, k * 13 % 255, 90))
            return {"ok": True, "bytes": data, "mime": "image/png", "cost": 0.03}
        finally:
            with self.lock:
                self.active -= 1


def wait(timeout=20):
    t = ab._JOB["thread"]
    if t is not None:
        t.join(timeout)
        assert not t.is_alive(), "the background job never finished"


def connect_openrouter():
    ok, _ = providers.create_api_key(WS, "openrouter", "OpenRouter", "sk-or-test", UID)
    assert ok


def disconnect():
    store.write(lambda c: c.execute("UPDATE models SET provider_id=NULL"))
    store.write(lambda c: c.execute("DELETE FROM providers"))


class Base(unittest.TestCase):
    def setUp(self):
        disconnect()
        connect_openrouter()
        config.set("workspace", WS, "image_model", "bytedance-seed/seedream-5-0-lite")
        config.set("workspace", WS, "image_content_precheck_enabled", True)
        self.tmp = Path(tempfile.mkdtemp(prefix="nori_avbuild_live_"))
        self.avatars = self.tmp / "avatars"
        (self.avatars / "source").mkdir(parents=True)
        for p in (patch.object(ab, "AVATAR_DIR", self.avatars), patch.object(imagegen, "_REFERENCE_PATH", self.avatars / "source" / "neutral.png"),
                  patch.object(server, "AVATAR_DIR", self.avatars), patch.object(server, "AVATAR_THUMB_DIR", self.avatars / "thumbs")):
            p.start()
            self.addCleanup(p.stop)
        shutil.rmtree(ab.BUILD_DIR, ignore_errors=True)
        shutil.rmtree(ab.BACKUP_DIR, ignore_errors=True)
        self.fake = FakeImages()
        p = patch.object(chat, "openrouter_image", self.fake)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def candidates(self):
        ok, msg = ab.start(WS, UID, DESC)
        self.assertTrue(ok, msg)
        wait()

    def chosen(self):
        self.candidates()
        self.assertTrue(ab.pick(1)[0])

    def full_set(self):
        self.chosen()
        self.assertTrue(ab.build_set(WS, UID)[0])
        wait()
        self.assertTrue(ab.counts(ab.status())["complete"])

    def old_avatars(self, only=None):
        """The original character's files, as they sit in the live folder before a build is installed."""
        for s in only or emotion.STATES:
            (self.avatars / f"{s}.png").write_bytes(b"OLD-" + s.encode())
        (self.avatars / "source" / "neutral.png").write_bytes(b"OLD-SOURCE")
        (self.avatars / "thumbs").mkdir(exist_ok=True)
        for s in only or emotion.STATES:
            (self.avatars / "thumbs" / f"{s}.jpg").write_bytes(b"OLD-THUMB")


class Prompts(unittest.TestCase):
    def test_every_state_has_a_look_and_it_reaches_the_prompt(self):
        self.assertEqual(set(emotion.LOOKS), set(emotion.STATES))
        for s in emotion.STATES:
            self.assertIn(emotion.LOOKS[s], ab.state_prompt(s))

    def test_a_state_without_a_look_still_gets_a_prompt(self):
        self.assertIn("being whimsical", emotion.look("whimsical"))

    def test_the_prompts_carry_the_adult_text_and_the_description(self):
        self.assertIn(DESC.rstrip("."), ab.neutral_prompt(DESC))
        for p in (ab.neutral_prompt(DESC), ab.state_prompt("happy")):
            self.assertIn("Nothing depicting or implying a minor", p)
            self.assertIn("No nudity", p)

    def test_the_state_prompt_locks_identity_and_forbids_collages(self):
        p = ab.state_prompt("angry")
        self.assertIn("same character as in the reference image", p)
        self.assertIn("not a collage", p)

    def test_the_known_extensions_match_what_the_server_resolves(self):
        self.assertEqual(ab.EXTS, tuple(server._AVATAR_EXTS))


class Readiness(Base):
    def test_a_connected_openrouter_provider_and_a_model_is_ready(self):
        self.assertTrue(ab.readiness(WS)["ready"])

    def test_no_key_is_not_ready(self):
        disconnect()
        r = ab.readiness(WS)
        self.assertFalse(r["key"])
        self.assertFalse(r["ready"])

    def test_a_key_in_the_environment_alone_is_ready(self):
        disconnect()
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-env"}):
            self.assertTrue(ab.readiness(WS)["ready"])

    def test_a_blank_image_model_is_not_ready(self):
        config.set("workspace", WS, "image_model", " ")
        self.assertFalse(ab.readiness(WS)["ready"])

    def test_a_non_openrouter_provider_does_not_count(self):
        disconnect()
        providers.create_api_key(WS, "anthropic_api_key", "Anthropic", "sk-ant", UID)
        self.assertFalse(ab.readiness(WS)["key"])

    def test_start_is_refused_when_not_ready(self):
        disconnect()
        ok, msg = ab.start(WS, UID, DESC)
        self.assertFalse(ok)
        self.assertIn("OpenRouter", msg)
        self.assertEqual(self.fake.calls, [])
        self.assertIsNone(ab.status())


class ImageKey(Base):
    """The wizard stores keys in the providers table; image and embedding calls used to read only the environment, so they could never run there."""

    def _header_sent(self, fn):
        seen = {}

        class Resp:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return b'{"data":[{"b64_json":"aGk="}]}'

        def fake_urlopen(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            return Resp()
        with patch.object(chat.urllib.request, "urlopen", fake_urlopen):
            fn()
        return seen["auth"]

    def test_images_use_the_connected_providers_key(self):
        self.addCleanup(patch.stopall)
        patch.stopall()  # the real openrouter_image, not the fake
        auth = self._header_sent(lambda: chat.openrouter_image("x", model="m"))
        self.assertEqual(auth, "Bearer sk-or-test")

    def test_the_provider_key_wins_over_the_environment(self):
        patch.stopall()
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-env"}):
            self.assertEqual(chat.openrouter_key(), "sk-or-test")

    def test_with_no_provider_the_environment_key_is_used(self):
        patch.stopall()
        disconnect()
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-env"}):
            self.assertEqual(chat.openrouter_key(), "sk-env")

    def test_with_neither_it_raises_and_the_image_call_reports_it(self):
        patch.stopall()
        disconnect()
        with self.assertRaises(chat.ModelError):
            chat.openrouter_key()
        res = chat.openrouter_image("x", model="m")
        self.assertFalse(res["ok"])


class Description(Base):
    def test_too_short_and_too_long_are_refused(self):
        self.assertFalse(ab.check_description("hi", WS)[0])
        self.assertFalse(ab.check_description("x" * (ab.DESC_MAX + 1), WS)[0])

    def test_minors_are_refused_by_the_precheck(self):
        ok, msg = ab.check_description("A teenager with long brown hair and a school uniform", WS)
        self.assertFalse(ok)
        self.assertIn("minors", msg)

    def test_the_precheck_setting_is_honoured(self):
        config.set("workspace", WS, "image_content_precheck_enabled", False)
        self.assertTrue(ab.check_description("A teenager with long brown hair and a school uniform", WS)[0])

    def test_a_flagged_description_draws_nothing(self):
        ok, _ = ab.start(WS, UID, "A naked woman with long hair standing in a garden")
        self.assertFalse(ok)
        self.assertEqual(self.fake.calls, [])

    def test_a_normal_description_is_accepted_and_trimmed(self):
        self.assertEqual(ab.check_description("  " + DESC + "  ", WS), (True, DESC))


class Candidates(Base):
    def test_three_candidates_are_drawn_without_a_reference(self):
        self.candidates()
        st = ab.status()
        self.assertEqual(len(self.fake.calls), 3)
        self.assertTrue(all(c["references"] is None for c in self.fake.calls))
        self.assertTrue(all(DESC.rstrip(".") in c["prompt"] for c in self.fake.calls))
        self.assertEqual([c["status"] for c in st["candidates"]], ["done"] * 3)
        self.assertFalse(st["busy"])
        self.assertEqual(st["images"], 3)
        self.assertAlmostEqual(st["cost_usd"], 0.09, places=4)
        for c in st["candidates"]:
            self.assertTrue((ab.BUILD_DIR / c["file"]).is_file())

    def test_the_model_is_the_configured_one(self):
        config.set("workspace", WS, "image_model", "some/other-model")
        self.candidates()
        self.assertEqual({c["model"] for c in self.fake.calls}, {"some/other-model"})

    def test_every_picture_is_logged_as_the_builder_with_its_cost(self):
        def builder_rows():
            return [r for r in medialog.recent_log(WS, 500) if r["purpose"] == "avatar_builder"]
        before = len(builder_rows())
        self.candidates()
        rows = builder_rows()[:len(builder_rows()) - before]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["ok"] and abs(r["cost_usd"] - 0.03) < 1e-9 for r in rows))

    def test_the_builders_spend_is_in_the_total_but_not_todays_household_cap(self):
        before = medialog.spend_status(WS, UID)
        self.candidates()
        after = medialog.spend_status(WS, UID)
        self.assertAlmostEqual(after["spent_total_usd"] - before["spent_total_usd"], 0.09, places=4)
        self.assertAlmostEqual(after["spent_today_usd"], before["spent_today_usd"], places=4)

    def test_a_failed_candidate_shows_a_clean_message_and_the_others_still_land(self):
        calls = {"n": 0}

        def flaky(prompt):
            calls["n"] += 1
            return calls["n"] == 2
        self.fake.fail_if = flaky
        self.candidates()
        statuses = sorted(c["status"] for c in ab.status()["candidates"])
        self.assertEqual(statuses, ["done", "done", "failed"])
        self.assertIn("image generation failed", [c for c in ab.status()["candidates"] if c["status"] == "failed"][0]["error"])

    def test_something_that_is_not_an_image_is_a_failed_picture(self):
        self.fake.bytes_for = lambda prompt: b"<html>not an image</html>"
        self.candidates()
        st = ab.status()
        self.assertEqual([c["status"] for c in st["candidates"]], ["failed"] * 3)
        self.assertEqual(st["cost_usd"], 0)

    def test_a_crash_in_the_image_call_is_a_failed_picture_not_a_stuck_one(self):
        def boom(prompt, **kw):
            raise RuntimeError("kaboom")
        with patch.object(chat, "openrouter_image", boom):
            self.candidates()
        st = ab.status()
        self.assertEqual([c["status"] for c in st["candidates"]], ["failed"] * 3)
        self.assertFalse(st["busy"])

    def test_more_candidates_redraws_all_three_from_the_same_description(self):
        self.candidates()
        first = [c["file"] for c in ab.status()["candidates"]]
        ok, _ = ab.more_candidates(WS, UID)
        self.assertTrue(ok)
        wait()
        self.assertEqual(len(self.fake.calls), 6)
        self.assertEqual(ab.status()["description"], DESC)
        self.assertEqual([c["file"] for c in ab.status()["candidates"]], first)

    def test_a_second_job_while_one_runs_is_refused(self):
        self.fake.gate = threading.Event()
        ok, _ = ab.start(WS, UID, DESC)
        self.assertTrue(ok)
        try:
            ok2, msg = ab.start(WS, UID, DESC)
            self.assertFalse(ok2)
            self.assertIn("already running", msg)
            self.assertTrue(ab.status()["busy"])
            self.assertFalse(ab.pick(0)[0], "no choosing while pictures are still being drawn")
        finally:
            self.fake.gate.set()
            wait()


class ChoosingAndBuilding(Base):
    def test_picking_makes_that_picture_the_neutral_state(self):
        self.candidates()
        cand = ab.status()["candidates"][1]["file"]
        self.assertTrue(ab.pick(1)[0])
        st = ab.status()
        self.assertEqual(st["chosen"], 1)
        self.assertEqual((ab.BUILD_DIR / st["states"]["neutral"]["file"]).read_bytes(), (ab.BUILD_DIR / cand).read_bytes())
        self.assertEqual({s["status"] for n, s in st["states"].items() if n != "neutral"}, {"pending"})
        self.assertEqual(sorted(st["states"]), sorted(emotion.STATES))

    def test_bad_choices_are_refused(self):
        self.candidates()
        for bad in (-1, 3, 99, "1", None):
            self.assertFalse(ab.pick(bad)[0], bad)
        self.assertIsNone(ab.status()["chosen"])

    def test_the_face_can_be_changed_until_the_rest_is_drawn(self):
        self.chosen()
        self.assertTrue(ab.unpick()[0])
        self.assertIsNone(ab.status()["chosen"])
        self.assertTrue(ab.pick(2)[0])
        self.assertTrue(ab.build_set(WS, UID)[0])
        wait()
        self.assertFalse(ab.unpick()[0])

    def test_building_draws_every_other_state_from_the_chosen_face(self):
        self.chosen()
        self.fake.calls.clear()
        ok, msg = ab.build_set(WS, UID)
        self.assertTrue(ok, msg)
        wait()
        self.assertEqual(len(self.fake.calls), len(OTHERS))
        neutral_bytes = (ab.BUILD_DIR / ab.status()["states"]["neutral"]["file"]).read_bytes()
        for call in self.fake.calls:
            self.assertEqual(len(call["references"]), 1)
            self.assertEqual(call["references"][0][0], neutral_bytes, "the chosen neutral face is the reference")
            self.assertEqual(call["references"][0][1], "image/png")
        drawn = {s for s in OTHERS if any(emotion.LOOKS[s] in c["prompt"] for c in self.fake.calls)}
        self.assertEqual(drawn, set(OTHERS), "each state's own look was asked for exactly once")
        st = ab.status()
        self.assertTrue(ab.counts(st)["complete"])
        self.assertEqual(ab.counts(st)["total"], len(emotion.STATES))

    def test_they_are_drawn_in_parallel_but_never_more_than_the_limit(self):
        self.chosen()
        self.fake.calls.clear()
        ab.build_set(WS, UID)
        wait()
        self.assertGreater(self.fake.max_active, 1)
        self.assertLessEqual(self.fake.max_active, ab.WORKERS)

    def test_a_failed_picture_is_retried_alone_and_nothing_else_is_redrawn(self):
        self.chosen()
        self.fake.fail_if = lambda prompt: emotion.LOOKS["angry"] in prompt
        ab.build_set(WS, UID)
        wait()
        st = ab.status()
        self.assertEqual(st["states"]["angry"]["status"], "failed")
        self.assertEqual(ab.counts(st)["failed"], 1)
        self.assertFalse(ab.install(WS, False)[0], "an incomplete set is never installed")
        self.fake.fail_if = lambda prompt: False
        self.fake.calls.clear()
        self.assertTrue(ab.build_set(WS, UID)[0])
        wait()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIn(emotion.LOOKS["angry"], self.fake.calls[0]["prompt"])
        self.assertTrue(ab.counts(ab.status())["complete"])

    def test_building_when_everything_is_drawn_does_nothing(self):
        self.full_set()
        self.fake.calls.clear()
        self.assertFalse(ab.build_set(WS, UID)[0])
        self.assertEqual(self.fake.calls, [])

    def test_redrawing_one_state_replaces_only_that_picture(self):
        self.full_set()
        before = {n: (ab.BUILD_DIR / s["file"]).read_bytes() for n, s in ab.status()["states"].items()}
        self.fake.calls.clear()
        self.assertTrue(ab.redraw(WS, UID, "happy")[0])
        wait()
        self.assertEqual(len(self.fake.calls), 1)
        after = {n: (ab.BUILD_DIR / s["file"]).read_bytes() for n, s in ab.status()["states"].items()}
        self.assertNotEqual(after["happy"], before["happy"])
        self.assertEqual({n for n in after if after[n] != before[n]}, {"happy"})

    def test_neutral_and_unknown_states_cannot_be_redrawn(self):
        self.full_set()
        for bad in ("neutral", "nonsense", "", "../x"):
            self.assertFalse(ab.redraw(WS, UID, bad)[0], bad)

    def test_a_restart_mid_build_leaves_retryable_pictures(self):
        self.chosen()
        def apply(s):
            s["busy"], s["job"] = True, "set"
            s["states"]["happy"]["status"] = "running"
        ab._update(apply)
        self.assertFalse(ab._alive())
        st = ab.status()
        self.assertFalse(st["busy"])
        self.assertEqual(st["states"]["happy"]["status"], "pending")
        self.assertTrue(ab.build_set(WS, UID)[0])
        wait()

    def test_discard_deletes_the_draft(self):
        self.chosen()
        self.assertTrue(ab.discard()[0])
        self.assertIsNone(ab.status())
        self.assertFalse(ab.BUILD_DIR.exists())

    def test_discard_while_drawing_stops_and_then_wipes(self):
        self.fake.gate = threading.Event()
        ab.start(WS, UID, DESC)
        time.sleep(0.2)
        ok, msg = ab.discard()
        self.assertTrue(ok)
        self.assertIn("stopping", msg)
        self.fake.gate.set()
        wait()
        self.assertIsNone(ab.status())


class Install(Base):
    def test_install_moves_the_old_avatars_aside_and_puts_the_new_ones_live(self):
        self.old_avatars()
        self.full_set()
        ok, msg = ab.install(WS, False)
        self.assertTrue(ok, msg)
        st = ab.status()
        for s in emotion.STATES:
            live = self.avatars / st["states"][s]["file"]
            self.assertEqual(live.read_bytes(), (ab.BUILD_DIR / st["states"][s]["file"]).read_bytes())
            self.assertFalse((self.avatars / "thumbs" / f"{s}.jpg").exists(), "the cached thumbnail of the old picture is gone")
        bak = ab.latest_backup()
        self.assertIsNotNone(bak)
        self.assertEqual((bak / "avatars" / "happy.png").read_bytes(), b"OLD-happy")
        self.assertEqual((bak / "source" / "neutral.png").read_bytes(), b"OLD-SOURCE")
        self.assertEqual((self.avatars / "source" / "neutral.png").read_bytes(),
                         (ab.BUILD_DIR / st["states"]["neutral"]["file"]).read_bytes(), "the neutral face is now the selfie reference")
        self.assertTrue(ab.status()["installed_ts"])

    def test_an_old_file_in_another_format_cannot_shadow_a_new_one(self):
        """png is checked first: an old happy.png left behind would hide a new happy.webp."""
        self.old_avatars()
        self.fake.bytes_for = lambda prompt: png(fmt="WEBP")
        self.fake_mime = None
        with patch.object(chat, "openrouter_image", lambda p, **kw: {"ok": True, "bytes": png(fmt="WEBP"), "mime": "image/webp", "cost": 0.03}):
            self.full_set()
        ab.install(WS, False)
        self.assertFalse((self.avatars / "happy.png").exists())
        self.assertTrue((self.avatars / "happy.webp").is_file())
        p, _ = server.Handler._find_avatar_file("happy")
        self.assertEqual(p.name, "happy.webp")

    def test_install_refuses_an_incomplete_set_and_changes_nothing(self):
        self.old_avatars()
        self.chosen()
        ok, msg = ab.install(WS, False)
        self.assertFalse(ok)
        self.assertEqual((self.avatars / "happy.png").read_bytes(), b"OLD-happy")
        self.assertIsNone(ab.latest_backup())

    def test_the_selfie_text_is_rewritten_only_when_asked(self):
        old_prefix = config.get("workspace", WS, "image_style_prefix")
        self.full_set()
        ab.install(WS, False)
        self.assertEqual(config.get("workspace", WS, "image_style_prefix"), old_prefix)
        self.setUp()  # a second build
        self.full_set()
        ab.install(WS, True)
        prefix = config.get("workspace", WS, "image_style_prefix")
        self.assertIn(DESC.rstrip("."), prefix)
        self.assertNotIn("dark brown wavy hair", prefix, "the original character's description is replaced")
        constraint = config.get("workspace", WS, "image_clothed_constraint")
        self.assertIn("minor", constraint)
        self.assertNotIn("late 20s", constraint)

    def test_restore_puts_everything_back(self):
        self.old_avatars()
        old_prefix = config.get("workspace", WS, "image_style_prefix")
        old_constraint = config.get("workspace", WS, "image_clothed_constraint")
        self.full_set()
        ab.install(WS, True)
        ok, msg = ab.restore(WS)
        self.assertTrue(ok, msg)
        for s in emotion.STATES:
            self.assertEqual((self.avatars / f"{s}.png").read_bytes(), b"OLD-" + s.encode())
            self.assertEqual([p.name for p in ab._avatar_files(s)], [f"{s}.png"], "no file of the new set is left behind")
        self.assertEqual((self.avatars / "source" / "neutral.png").read_bytes(), b"OLD-SOURCE")
        self.assertEqual(config.get("workspace", WS, "image_style_prefix"), old_prefix)
        self.assertEqual(config.get("workspace", WS, "image_clothed_constraint"), old_constraint)
        self.assertFalse(ab.has_backup(), "a backup is restored once")
        self.assertFalse(ab.restore(WS)[0])

    def test_two_installs_keep_two_backups_and_restore_takes_the_latest(self):
        self.old_avatars()
        self.full_set()
        ab.install(WS, False)
        first_live = (self.avatars / "happy.png").read_bytes()
        ab.discard()
        self.full_set()
        ab.install(WS, False)
        self.assertNotEqual((self.avatars / "happy.png").read_bytes(), first_live)
        self.assertEqual(len(list(ab.BACKUP_DIR.iterdir())), 2)
        self.assertTrue(ab.restore(WS)[0])
        self.assertEqual((self.avatars / "happy.png").read_bytes(), first_live, "restore undoes the latest install, not the first")
        self.assertTrue(ab.has_backup(), "the older backup is still there")

    def test_restore_with_nothing_saved_is_refused(self):
        self.assertFalse(ab.restore(WS)[0])


class _H(server.Handler):
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.sent = []

    def send(self, code, body=b"", extra=None, *, ctype="text/html; charset=utf-8"):
        self.sent.append({"code": code, "body": body, "extra": extra or {}, "ctype": ctype})

    def forbidden(self):
        self.sent.append({"code": 403, "body": b"", "extra": {}, "ctype": ""})

    def not_found(self):
        self.sent.append({"code": 404, "body": b"", "extra": {}, "ctype": ""})

    def html(self):
        b = self.sent[-1]["body"]
        return b.decode("utf-8") if isinstance(b, bytes) else b


def page(sess=ADMIN, **q):
    h = _H()
    h.avatar_builder_page(dict(sess), {k: [v] for k, v in q.items()})
    return h


def post(action, sess=ADMIN, **form):
    h = _H()
    h.avatar_build_post(dict(sess), f"/admin/avatars/build/{action}", dict(form))
    return h


class Pages(Base):
    def test_the_page_is_admin_only(self):
        self.assertEqual(page(MEMBER).sent[-1]["code"], 403)
        self.assertEqual(post("describe", MEMBER, description=DESC).sent[-1]["code"], 403)
        self.assertEqual(self.fake.calls, [])
        h = _H()
        h.avatar_build_img_get(dict(MEMBER), "cand_0.png", {})
        self.assertEqual(h.sent[-1]["code"], 403)

    def test_the_form_is_offered_when_ready_and_the_connect_card_when_not(self):
        html = page().html()
        self.assertIn("Describe her face", html)
        self.assertIn("action='/admin/avatars/build/describe'", html)
        self.assertIn(f"${3 * ab.per_image_estimate(WS):.2f}", html, "the estimate (3 x the per-image cost) is shown before anything is spent")
        disconnect()
        html = page().html()
        self.assertIn("Connect an image model first", html)
        self.assertIn("/settings?tab=models", html)
        self.assertNotIn("Describe her face", html)

    def test_from_the_wizard_the_back_link_and_connect_link_stay_in_the_wizard(self):
        disconnect()
        html = page(**{"from": "firstrun"}).html()
        self.assertIn("href='/first-run?step=identity'", html)
        self.assertIn("href='/first-run?step=connect'", html)
        providers.create_api_key(WS, "openai_api_key", "OpenAI", "sk-x", UID)   # with a provider to choose, the setup form appears and stays in the wizard
        self.assertIn("name=from value=firstrun", page(**{"from": "firstrun"}).html())

    def test_describing_starts_the_job_and_redirects_back(self):
        h = post("describe", description=DESC)
        self.assertEqual(h.sent[-1]["code"], 303)
        self.assertTrue(h.sent[-1]["extra"]["Location"].startswith("/admin/avatars/build"))
        wait()
        self.assertEqual(len(self.fake.calls), 3)

    def test_a_refusal_comes_back_as_an_error_banner_not_a_crash(self):
        h = post("describe", description="x")
        loc = h.sent[-1]["extra"]["Location"]
        self.assertIn("err=", loc)
        html = page(err="that was refused <b>").html()
        self.assertIn("that was refused &lt;b&gt;", html)

    def test_the_wizard_origin_survives_the_redirect(self):
        h = post("describe", **{"description": DESC, "from": "firstrun"})
        self.assertIn("from=firstrun", h.sent[-1]["extra"]["Location"])
        wait()

    def test_the_candidate_page_offers_a_choice_and_the_expression_page_a_grid(self):
        self.candidates()
        html = page().html()
        self.assertEqual(html.count("action='/admin/avatars/build/pick'"), 3)
        self.assertIn("Choose her neutral face", html)
        post("pick", index="0")
        html = page().html()
        self.assertIn("Draw the other 23 expressions", html)
        self.assertIn("neutral: her reference face", html)
        for s in OTHERS:
            self.assertIn(f">{s}</p>", html)
        post("build")
        wait()
        html = page().html()
        self.assertIn("24 of 24 drawn", html)
        self.assertIn("Install these avatars", html)
        self.assertEqual(html.count("action='/admin/avatars/build/redo'"), len(OTHERS))

    def test_while_drawing_the_page_refreshes_itself_and_hides_the_buttons(self):
        self.fake.gate = threading.Event()
        ab.start(WS, UID, DESC)
        try:
            html = page().html()
            self.assertIn("location.reload()", html)
            self.assertIn("Drawing in the background", html)
        finally:
            self.fake.gate.set()
            wait()
        self.assertNotIn("location.reload()", page().html())

    def test_pick_with_a_garbage_index_is_an_error_not_a_crash(self):
        self.candidates()
        h = post("pick", index="banana")
        self.assertIn("err=", h.sent[-1]["extra"]["Location"])

    def test_saving_the_image_model(self):
        post("model", image_model="  vendor/model-x ")
        self.assertEqual(config.get("workspace", WS, "image_model"), "vendor/model-x")
        post("model", image_model="  ")     # blank means the provider's default
        self.assertEqual(config.get("workspace", WS, "image_model"), chat.IMAGE_DEFAULT_MODELS["openrouter"])
        self.assertIn("err=", post("model", image_provider="9999").sent[-1]["extra"]["Location"])

    def test_the_restore_button_appears_only_when_there_is_something_to_restore(self):
        self.assertNotIn("Restore the earlier avatars", page().html())
        self.old_avatars()
        self.full_set()
        ab.install(WS, False)
        self.assertIn("Restore the earlier avatars", page().html())

    def test_the_avatars_tab_and_the_wizard_both_link_to_the_builder(self):
        h = _H()
        h._settings_response = lambda sess, tab, body, **kw: h.sent.append({"code": 200, "body": body, "extra": {}, "ctype": ""})
        h.avatars_admin_page(dict(ADMIN))
        self.assertIn("href='/admin/avatars/build'", h.html())
        w = _H()
        w.first_run_page(dict(ADMIN), step="identity")
        self.assertIn("href='/admin/avatars/build?from=firstrun'", w.html())


class DraftImages(Base):
    def test_draft_pictures_are_served_to_the_admin_only_and_only_by_known_name(self):
        self.candidates()
        name = ab.status()["candidates"][0]["file"]
        h = _H()
        h.avatar_build_img_get(dict(ADMIN), name, {})
        self.assertEqual(h.sent[-1]["code"], 200)
        self.assertEqual(h.sent[-1]["ctype"], "image/png")
        h = _H()
        h.avatar_build_img_get(dict(ADMIN), name, {"size": ["sm"]})
        self.assertEqual(h.sent[-1]["ctype"], "image/jpeg")
        for bad in ("../state.json", "state.json", "..\\x", "/etc/passwd", "cand_9.png", "", ".hidden", "thumbs"):
            h = _H()
            h.avatar_build_img_get(dict(ADMIN), bad, {})
            self.assertEqual(h.sent[-1]["code"], 404, bad)
        self.assertIsNone(ab.draft_file("../state.json"))

    def test_a_redrawn_picture_gets_a_fresh_thumbnail(self):
        self.full_set()
        h = _H()
        h.avatar_build_img_get(dict(ADMIN), ab.status()["states"]["happy"]["file"], {"size": ["sm"]})
        first = h.sent[-1]["body"]
        time.sleep(1.1)
        ab.redraw(WS, UID, "happy")
        wait()
        h = _H()
        h.avatar_build_img_get(dict(ADMIN), ab.status()["states"]["happy"]["file"], {"size": ["sm"]})
        self.assertNotEqual(h.sent[-1]["body"], first)


class AvatarCaching(Base):
    def test_the_live_avatar_is_revalidated_not_cached_blind(self):
        self.old_avatars(["happy"])
        (self.avatars / "happy.png").write_bytes(png())
        h = _H()
        h.serve_avatar("happy")
        r = h.sent[-1]
        self.assertEqual(r["code"], 200)
        etag = r["extra"]["ETag"]
        self.assertEqual(r["extra"]["Cache-Control"], "no-cache")
        h = _H({"If-None-Match": etag})
        h.serve_avatar("happy")
        self.assertEqual(h.sent[-1]["code"], 304)
        # a new picture under the same name is a new tag, so an installed set shows up on the next load
        (self.avatars / "happy.png").write_bytes(png((1, 2, 3)))
        (self.avatars / "thumbs" / "happy.jpg").unlink()
        h = _H({"If-None-Match": etag})
        h.serve_avatar("happy")
        self.assertEqual(h.sent[-1]["code"], 200)
        self.assertNotEqual(h.sent[-1]["extra"]["ETag"], etag)

    def test_the_selfie_reference_mime_follows_the_file(self):
        (self.avatars / "source" / "neutral.png").write_bytes(png(fmt="WEBP"))
        data, mime = imagegen.reference_bytes()
        self.assertEqual(mime, "image/webp")
        (self.avatars / "source" / "neutral.png").write_bytes(png())
        self.assertEqual(imagegen.reference_bytes()[1], "image/png")


if __name__ == "__main__":
    unittest.main()

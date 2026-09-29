"""Adversarial proof for the path-safety fix (2026-09-23, the live-data incident: a contaminated multi-file test
run overwrote nori's real persona.md and its baseline for real, root-caused to a module-level path constant
computed once at import and never re-checked). This file does not trust the fix by reading it -- it actively
TRIES to make store.py, persona.py, promptdoc.py and guidance.py resolve to nori's REAL data/prompts directories,
the exact shape the real incident took, and asserts every attempt is refused, loudly, never silently corrected.
If any test here ever passes because the attack SUCCEEDED (a real path got returned, or a RuntimeError became a
quiet fallback), that is the bug this file exists to catch -- see each test's own assertion.

Deliberately does NOT set NORI_DATA_DIR/NORI_PROMPTS_DIR at import time, unlike every other test file -- that
would defeat the point. Every test manages the exact env vars it's attacking and restores them in tearDown."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store  # noqa: E402
import persona  # noqa: E402
import promptdoc  # noqa: E402
import guidance  # noqa: E402

_ENV_KEYS = ("NORI_DATA_DIR", "NORI_LIVE", "NORI_PROMPTS_DIR")


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class UnconfiguredRefusesLoudly(_Base):
    def test_store_data_dir_refuses(self):
        with self.assertRaises(RuntimeError):
            store.DATA_DIR

    def test_store_db_path_refuses(self):
        with self.assertRaises(RuntimeError):
            store.DB_PATH

    def test_persona_refuses(self):
        with self.assertRaises(RuntimeError):
            persona.PERSONA_PATH

    def test_persona_load_prompt_refuses_not_silently_empty(self):
        with self.assertRaises(RuntimeError):
            persona.load_prompt()

    def test_promptdoc_refuses(self):
        doc = promptdoc.PromptDoc("tool-usage")
        with self.assertRaises(RuntimeError):
            doc.path


class AdversarialRealPathAttack(_Base):
    """THE attack this file exists to run: point every resolver at the REAL live directory, the exact shape the
    real incident took (tests/test_nori_tuning_admin.py's own NORI_PROMPTS_DIR override silently ignored because
    persona/promptdoc were already imported earlier in the same process) -- WITHOUT NORI_LIVE. A test in this
    class that passes by actually returning the real path means this file is reporting a fixed bug as broken
    again -- treat that as more serious than any other failure in this repo."""

    def test_data_dir_pointed_at_the_real_directory_is_refused(self):
        os.environ["NORI_DATA_DIR"] = str(store._REAL_DATA_DIR)
        with self.assertRaises(RuntimeError):
            store.DATA_DIR

    def test_connect_pointed_at_the_real_directory_is_refused_before_any_file_touches_disk(self):
        os.environ["NORI_DATA_DIR"] = str(store._REAL_DATA_DIR)
        with self.assertRaises(RuntimeError):
            store.connect()

    def test_prompts_dir_pointed_at_the_real_directory_is_refused(self):
        os.environ["NORI_PROMPTS_DIR"] = str(promptdoc._REAL_PROMPTS_DIR)
        with self.assertRaises(RuntimeError):
            persona.PERSONA_PATH

    def test_persona_save_pointed_at_the_real_directory_is_refused_before_writing(self):
        """The exact shape of the real incident, run again against the now-fixed code: save(), with the env
        pointed at the real prompts directory. Must refuse before _atomic_write ever runs -- prove nothing landed
        on disk by checking the real file's own mtime and content are untouched."""
        real_file = promptdoc._REAL_PROMPTS_DIR / "persona.md"
        before = real_file.stat().st_mtime if real_file.is_file() else None
        before_text = real_file.read_text(encoding="utf-8") if real_file.is_file() else None
        os.environ["NORI_PROMPTS_DIR"] = str(promptdoc._REAL_PROMPTS_DIR)
        with self.assertRaises(RuntimeError):
            persona.save("=== PROMPT STARTS ===\nADVERSARIAL TEST WRITE -- if you see this in the real "
                         "persona.md, the guard failed.\n=== PROMPT ENDS ===\n")
        after = real_file.stat().st_mtime if real_file.is_file() else None
        after_text = real_file.read_text(encoding="utf-8") if real_file.is_file() else None
        self.assertEqual(before, after, "the real persona.md's mtime changed -- the adversarial write got through")
        self.assertEqual(before_text, after_text, "the real persona.md's content changed -- the adversarial write got through")
        if after_text:
            self.assertNotIn("ADVERSARIAL TEST WRITE", after_text)

    def test_promptdoc_pointed_at_the_real_directory_is_refused(self):
        os.environ["NORI_PROMPTS_DIR"] = str(promptdoc._REAL_PROMPTS_DIR)
        doc = promptdoc.PromptDoc("tool-usage")
        with self.assertRaises(RuntimeError):
            doc.path

    def test_guidance_pointed_at_the_real_directory_is_refused(self):
        # guidance.py has no module-level PATH (no external consumer ever needed one) -- exercise the same
        # underlying PromptDoc instance it actually uses instead.
        os.environ["NORI_PROMPTS_DIR"] = str(promptdoc._REAL_PROMPTS_DIR)
        with self.assertRaises(RuntimeError):
            guidance._doc.path


class TheLiveFlagIsWhatActuallyAuthorizesIt(_Base):
    """The other side of the same coin: with NORI_LIVE set (what nori_ctl.ps1 sets for the real service), the
    real path IS the correct, intended answer -- the guard must not block the real service."""

    def test_data_dir_resolves_to_the_real_directory_when_live_is_set(self):
        os.environ["NORI_LIVE"] = "1"
        self.assertEqual(store.DATA_DIR, store._REAL_DATA_DIR)

    def test_prompts_dir_resolves_to_the_real_directory_when_live_is_set(self):
        os.environ["NORI_LIVE"] = "1"
        self.assertEqual(persona.PERSONA_PATH.parent, promptdoc._REAL_PROMPTS_DIR)

    def test_an_explicit_scratch_override_still_works_normally_alongside_live(self):
        import tempfile
        scratch = tempfile.mkdtemp(prefix="nori_path_safety_")
        os.environ["NORI_DATA_DIR"] = scratch
        self.assertEqual(str(store.DATA_DIR), scratch)


class LivePathsAreNeverEqualByAccident(unittest.TestCase):
    def test_real_data_dir_is_the_apps_own_data_folder(self):
        self.assertEqual(store._REAL_DATA_DIR, Path(store.__file__).resolve().parent / "data")

    def test_real_prompts_dir_is_the_apps_own_prompts_folder(self):
        self.assertEqual(promptdoc._REAL_PROMPTS_DIR, Path(promptdoc.__file__).resolve().parent / "prompts")


if __name__ == "__main__":
    unittest.main()

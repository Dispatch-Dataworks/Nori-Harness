"""Image-model dropdown: list_image_models filters to models that can draw."""
import os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_imgmodels_")
os.environ["NORI_NO_LOGFILE"] = "1"
import providers


class ImageModelListTests(unittest.TestCase):
    def test_openrouter_keeps_image_output_only(self):
        data = {"data": [
            {"id": "a/draw", "name": "Draw", "architecture": {"output_modalities": ["image"], "input_modalities": ["text", "image"]}},
            {"id": "a/chat", "name": "Chat", "architecture": {"output_modalities": ["text"], "input_modalities": ["text"]}},
            {"id": "b/both", "name": "Both", "architecture": {"output_modalities": ["text", "image"], "input_modalities": ["text"]}}]}
        with patch.object(providers, "_post_json", return_value=data), patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}):
            ok, items = providers.list_image_models(None)
        self.assertTrue(ok)
        self.assertEqual([m["id"] for m in items], ["a/draw", "b/both"])
        self.assertIn("reference", items[0]["label"])

    def test_openai_filters_image_ids(self):
        with patch.object(providers, "api_key_for", return_value="k"), patch.object(
                providers, "_list_models_openai", return_value=(True, [{"id": "gpt-4o", "label": "gpt-4o"}, {"id": "gpt-image-1", "label": "gpt-image-1"}, {"id": "dall-e-3", "label": "dall-e-3"}])):
            ok, items = providers.list_image_models({"type": "openai_api_key"})
        self.assertEqual([m["id"] for m in items], ["gpt-image-1", "dall-e-3"])

    def test_other_types_refused(self):
        ok, msg = providers.list_image_models({"type": "anthropic_api_key"})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()

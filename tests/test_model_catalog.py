import re
import unittest
from pathlib import Path


EXISTING_MODEL_MAP = {
    "gpt-latest": "openai/gpt-5.5",
    "gpt-5.5": "openai/gpt-5.5",
    "gpt-5.5-thinking": "openai/gpt-5.5",
    "gpt-5.4": "openai/gpt-5.4",
    "gpt-5.4-mini": "openai/gpt-5.4-mini",
    "gpt-5.4-nano": "openai/gpt-5.4-nano",
    "gpt-image-1.5": "openai/gpt-image-1.5",
    "gpt-image-2": "openai/gpt-image-2",
    "gemini-latest": "vertex_ai/gemini-3.1-pro-preview",
    "gemini-3.1-pro": "vertex_ai/gemini-3.1-pro-preview",
    "gemini-3.1-pro-customtools": "vertex_ai/gemini-3.1-pro-preview-customtools",
    "gemini-3.5-flash": "vertex_ai/gemini-3.5-flash",
    "gemini-3-flash-preview": "vertex_ai/gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview": "vertex_ai/gemini-3.1-flash-lite-preview",
    "nano-banana": "vertex_ai/gemini-3.1-flash-image",
    "nano-banana-2": "vertex_ai/gemini-3.1-flash-image",
    "nano-banana-pro": "vertex_ai/gemini-3-pro-image",
    "nano-banana-2-lite": "vertex_ai/gemini-3.1-flash-lite-image",
    "veo-3.1": "vertex_ai/veo-3.1-generate-001",
    "veo-3.1-fast": "vertex_ai/veo-3.1-fast-generate-001",
    "veo-3.1-lite": "vertex_ai/veo-3.1-lite-generate-001",
    "gemini-omni-flash": "vertex_ai/gemini-omni-flash-preview",
    "imagen-4.0": "vertex_ai/imagen-4.0-generate-001",
    "imagen-4.0-fast": "vertex_ai/imagen-4.0-fast-generate-001",
    "imagen-4.0-ultra": "vertex_ai/imagen-4.0-ultra-generate-001",
    "grok-video": "grok-video/grok-imagine-video",
    "grok-video-1.5": "grok-video/grok-imagine-video-1.5",
    "grok-imagine-video-1.5-2026-05-30": "grok-video/grok-imagine-video-1.5-2026-05-30",
    "grok-image": "grok-image/grok-imagine-image-quality",
    "grok-imagine-image-quality": "grok-image/grok-imagine-image-quality",
    "seedance-2.0": "seedance/dreamina-seedance-2-0-260128",
    "seedance-2.0-fast": "seedance/dreamina-seedance-2-0-fast-260128",
    "seedance-2.5": "seedance/dreamina-seedance-2-5-260628",
    "seedream-5.0": "seedream/seedream-5-0-260128",
    "seedream-5.0-lite": "seedream/seedream-5-0-lite-260128",
    "seedream-5.0-pro": "seedream/dola-seedream-5-0-pro-260628",
    "grok-latest": "xai/grok-4.20-non-reasoning-latest",
    "grok-4.20-reasoning": "xai/grok-4.20-reasoning-latest",
    "grok-4.20": "xai/grok-4.20-non-reasoning-latest",
    "elevenlabs-v3-tts": "elevenlabs/eleven_v3",
    "elevenlabs-multilingual-v2": "elevenlabs/eleven_multilingual_v2",
    "elevenlabs-sfx": "audio-studio/elevenlabs-sfx",
    "elevenlabs-music": "audio-studio/elevenlabs-music",
}


def catalog_from_config() -> dict[str, str]:
    text = Path(__file__).parents[1].joinpath("litellm_config.yaml").read_text()
    blocks = re.split(r"(?=^  - model_name:)", text, flags=re.MULTILINE)
    catalog: dict[str, str] = {}
    for block in blocks:
        name = re.search(r"^  - model_name:\s*(\S+)\s*$", block, flags=re.MULTILINE)
        model = re.search(r"^      model:\s*(\S+)\s*$", block, flags=re.MULTILINE)
        if name and model:
            catalog[name.group(1)] = model.group(1)
    return catalog


class ModelCatalogTests(unittest.TestCase):
    def test_existing_aliases_are_unchanged(self):
        catalog = catalog_from_config()
        self.assertEqual(
            {name: catalog.get(name) for name in EXISTING_MODEL_MAP},
            EXISTING_MODEL_MAP,
        )

    def test_new_aliases_have_exact_upstream_models(self):
        catalog = catalog_from_config()
        self.assertEqual(catalog["gpt-5.6-sol-medium"], "openai/gpt-5.6-sol")
        self.assertEqual(catalog["gpt-5.6-terra-medium"], "openai/gpt-5.6-terra")
        self.assertEqual(catalog["gpt-5.6-luna-medium"], "openai/gpt-5.6-luna")
        self.assertEqual(catalog["gpt-5.6-luna-high"], "openai/gpt-5.6-luna")
        self.assertEqual(catalog["gemini-3.7-flash"], "vertex_ai/gemini-3.7-flash")
        self.assertNotIn("gemini-3.6-flash", catalog)
        self.assertEqual(catalog["gemini-3.5-flash-lite"], "vertex_ai/gemini-3.5-flash-lite")
        self.assertEqual(
            catalog["gemini-omni-flash-preview"], "vertex_ai/gemini-omni-flash-preview"
        )
        self.assertEqual(
            catalog["grok-imagine-image-2.0"],
            "grok-image/grok-imagine-image-2.0",
        )
        self.assertNotIn("grok-image-2", catalog)


if __name__ == "__main__":
    unittest.main()

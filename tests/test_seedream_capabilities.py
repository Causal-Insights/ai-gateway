"""Exact Seedream Pro/Lite capability and byte-preserving edit contracts."""
import base64
import io
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import litellm
from litellm.types.utils import ImageResponse
import custom_handler_seedream as module
from custom_handler_seedream import SeedreamLLM, DEFAULT_MODEL_5_0_LITE as LITE, DEFAULT_MODEL_5_0_PRO as PRO
from gateway_request_policy import apply_request_policy

RealClient = httpx.AsyncClient


class SeedreamCapabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.key = patch.dict(os.environ, BYTEDANCE_API_KEY="offline-test")
        self.key.start()
        self.addCleanup(self.key.stop)
        self.handler = SeedreamLLM()
        self.sent = []
        self.provider_body = {"model": PRO, "data": [
            {"url": "https://example.test/base", "size": "2048x2048", "z_index": 0, "output_format": "jpeg"},
            {"url": "https://example.test/layer", "size": "1024x1024", "z_index": 1, "output_format": "png",
             "name": "title", "description": "title layer", "bounding_box": {"absolute": [1, 2, 3, 4], "normalized": [10, 20, 30, 40]}}],
            "usage": {"generated_images": 2, "input_images": 1}}
        async def provider(request):
            self.sent.append(json.loads(await request.aread()))
            return httpx.Response(200, json=self.provider_body)
        transport = httpx.MockTransport(provider)
        factory = lambda **kwargs: RealClient(transport=transport, **kwargs)
        self.http = patch.object(module, "httpx", SimpleNamespace(AsyncClient=factory, HTTPStatusError=httpx.HTTPStatusError))
        self.http.start()
        self.addCleanup(self.http.stop)

    def test_pro_layer_prompt_optional_presets_and_provider_restrictions(self):
        for size in ("1K", "1.5K", "2K", "auto"):
            body = {"model": "seedream-5.0-pro", "image": "https://example.test/input", "layer_decomposition": True, "size": size}
            normalized, error = apply_request_policy("/v1/images/generations", body)
            self.assertIsNone(error)
            self.assertEqual(normalized["prompt"], "")
            _, payload, _ = self.handler._prepare_request("", PRO, normalized)
            self.assertNotIn("prompt", payload)
            self.assertEqual(payload["size"], size)
            self.assertTrue(payload["layer_decomposition"])
        _, ordinary, _ = self.handler._prepare_request("fixture", PRO, {"size": "1.5K"})
        self.assertEqual(ordinary["size"], "1.5K")
        for params in ({"layer_decomposition": True}, {"image": ["a", "b"], "layer_decomposition": True},
                       {"image": "a", "layer_decomposition": True, "size": "2048x2048"},
                       {"stream": True}, {"sequential_image_generation": "auto"}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.handler._prepare_request("fixture", PRO, params)
        with self.assertRaisesRegex(ValueError, "only"):
            self.handler._prepare_request("fixture", LITE, {"layer_decomposition": True, "image": "a"})

    async def test_layers_preserve_all_assets_metadata_and_distinct_billing_units(self):
        result = await self.handler.aimage_generation(model=PRO, prompt="", model_response=ImageResponse(),
            api_key=None, api_base=None, optional_params={"image": "https://example.test/input", "layer_decomposition": True}, logging_obj=None)
        self.assertTrue(self.sent[0]["layer_decomposition"])
        self.assertNotIn("prompt", self.sent[0])
        self.assertEqual(len(result.data), 2)
        self.assertEqual(result.data[1].name, "title")
        self.assertEqual(result.model_dump()["data"][1]["name"], "title")
        self.assertEqual(result.model_dump()["data"][1]["provider_specific_fields"]["name"], "title")
        self.assertEqual(result.data[1].bounding_box, self.provider_body["data"][1]["bounding_box"])
        self.assertEqual([item.z_index for item in result.data], [0, 1])
        usage = result._hidden_params["gateway_usage"]
        self.assertEqual(usage["layer_images_small"], 1)
        self.assertEqual(usage["layer_images_large"], 1)
        self.assertEqual(usage["output_images_small"], 0)
        self.assertEqual(usage["output_images_large"], 0)
        self.assertEqual(usage["billable_reference_images"], 0)

    async def test_edit_preserves_order_raw_bytes_and_mime(self):
        refs = [("first.png", b"\x00first\xff", "image/png"), ("second.webp", io.BytesIO(b"second\x00"), "image/webp")]
        await self.handler.aimage_edit(model=LITE, image=refs, prompt="fixture", model_response=ImageResponse(),
            api_key=None, api_base=None, optional_params={"size": "2K"}, logging_obj=None)
        images = self.sent[0]["image"]
        self.assertTrue(images[0].startswith("data:image/png;base64,"))
        self.assertTrue(images[1].startswith("data:image/webp;base64,"))
        self.assertEqual([base64.b64decode(value.split(",", 1)[1]) for value in images], [b"\x00first\xff", b"second\x00"])
        self.assertEqual(self.sent[0]["model"], LITE)

    async def test_actual_pinned_custom_provider_edit_dispatch(self):
        with patch.object(litellm, "custom_provider_map", [{"provider": "seedream", "custom_handler": self.handler}]), \
             patch.object(litellm, "callbacks", []), patch.object(litellm, "provider_list", list(litellm.provider_list)), \
             patch.object(litellm, "_custom_providers", list(litellm._custom_providers)):
            from litellm.utils import custom_llm_setup
            custom_llm_setup()
            result = await litellm.aimage_edit(model="seedream/" + PRO, image=("fixture.png", b"raw-file", "image/png"),
                prompt="fixture", api_key="offline-test", size="1536x1536", num_retries=0)
        self.assertEqual(len(result.data), 2)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(base64.b64decode(self.sent[0]["image"][0].split(",", 1)[1]), b"raw-file")

    async def test_actual_pinned_generation_preserves_layer_and_transparency_controls(self):
        with patch.object(litellm, "custom_provider_map", [{"provider": "seedream", "custom_handler": self.handler}]), \
             patch.object(litellm, "callbacks", []), patch.object(litellm, "provider_list", list(litellm.provider_list)), \
             patch.object(litellm, "_custom_providers", list(litellm._custom_providers)):
            from litellm.utils import custom_llm_setup
            custom_llm_setup()
            for options in ({"layer_decomposition": True, "size": "auto"},
                            {"background": "transparent", "size": "1.5K", "prompt": "fixture"}):
                body, error = apply_request_policy("/v1/images/generations", {
                    "model": "seedream-5.0-pro", "image": "https://example.test/alpha.png", **options})
                self.assertIsNone(error)
                body["model"] = "seedream/" + PRO
                await litellm.aimage_generation(**body, api_key="offline-test", num_retries=0)
        self.assertEqual(self.sent[0]["size"], "auto")
        self.assertTrue(self.sent[0]["layer_decomposition"])
        self.assertNotIn("prompt", self.sent[0])
        self.assertEqual(self.sent[1]["background"], "transparent")
        self.assertEqual(self.sent[1]["size"], "1.5K")

    async def test_edit_mask_is_not_silently_dropped(self):
        with self.assertRaisesRegex(ValueError, "masks are unsupported"):
            await self.handler.aimage_edit(model=PRO, image=b"image", prompt="fixture", model_response=ImageResponse(),
                api_key=None, api_base=None, optional_params={"mask": b"mask"}, logging_obj=None)
        self.assertEqual(self.sent, [])

    def test_transparent_edit_constraints_and_optimizer_survive(self):
        params = {"image": "data:image/png;base64,YQ==", "background": "transparent", "optimize_prompt_options": {"mode": "fast"}}
        _, payload, _ = self.handler._prepare_request("fixture", PRO, params)
        self.assertEqual(payload["background"], "transparent")
        self.assertEqual(payload["optimize_prompt_options"], {"mode": "fast"})
        self.assertNotIn("output_format", payload)
        with self.assertRaisesRegex(ValueError, "requires png"):
            self.handler._prepare_request("fixture", PRO, {**params, "output_format": "jpeg"})

    def test_nonstream_partial_errors_and_missing_dimensions_preserved(self):
        body = {"data": [{"url": "https://example.test/ok"}, {"error": {"code": "moderated"}}]}
        result = self.handler._image_response_from_body(body)
        self.assertEqual(len(result.data), 1)
        self.assertEqual(result.errors, [{"image_index": 1, "error": {"code": "moderated"}}])
        self.assertNotIn("output_images_small", result._hidden_params["gateway_usage"])

    def test_layer_pricing_uses_actual_tiers_and_provider_reference_usage(self):
        from pricing_registry import PricingRegistry, PricingError
        from scripts.validate_price_evidence import validate
        from test_cost_accounting import price_document
        evidence = json.loads((Path(__file__).resolve().parents[1] /
            "pricing/evidence/dola-seedream-5-0-pro-260628-layers-2026-09-24.json").read_text())
        validate(evidence)
        prices = PricingRegistry(price_document())
        profile = prices.profiles["test-v1"]
        profile["components"] = [{"usage": row["name"], "rate": row["rate"], "per": row["per"], "unit": row["unit"]}
                                 for row in evidence["dimensions"]]
        body = {**self.provider_body, "_layer_decomposition": True, "_reference_count": 1,
                "usage": {"input_images": 3}}
        result = self.handler._image_response_from_body(body)
        usage = result._hidden_params["gateway_usage"]
        self.assertEqual(usage["billable_reference_images"], 2)
        self.assertEqual(prices.calculate(profile, usage)["cost_usd"], "0.0735")
        result = self.handler._image_response_from_body({"data": [{"url": "https://example.test/no-size"}],
            "_layer_decomposition": True, "_reference_count": 1})
        with self.assertRaises(PricingError):
            prices.calculate(profile, result._hidden_params["gateway_usage"])


if __name__ == "__main__":
    unittest.main()

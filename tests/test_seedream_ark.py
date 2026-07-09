"""
Tests for Seedream 5 ModelArk custom handler.

Gateway aliases (``litellm_config.yaml`` ``model_name``): ``seedream-5.0``,
``seedream-5.0-lite``. The handler receives the ModelArk upstream model id from
``litellm_params.model`` (after the ``seedream/`` prefix).

Uses unittest + mocks only. Stubs ``litellm`` so CI/local runs need not install it.
"""

import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Gateway aliases — what clients pass to POST /v1/images/generations
GATEWAY_MODEL_5_0 = "seedream-5.0"
GATEWAY_MODEL_5_0_LITE = "seedream-5.0-lite"

# ModelArk upstream ids — what the handler POSTs to /images/generations
ARK_MODEL_5_0 = "seedream-5-0-260128"
ARK_MODEL_5_0_LITE = "seedream-5-0-lite-260128"


def _ensure_litellm_stubs():
    if "litellm" in sys.modules:
        return

    class CustomLLM:  # noqa: D401
        """LiteLLM base (stub)."""

    class ImageObject:
        def __init__(self, url=None, b64_json=None, **kwargs):
            self.url = url
            self.b64_json = b64_json

    class ImageResponse:
        def __init__(self, created=0, data=None, **kwargs):
            self.created = created
            self.data = data if data is not None else []
            self._hidden_params = {}

    litellm = types.ModuleType("litellm")
    litellm.CustomLLM = CustomLLM
    sys.modules["litellm"] = litellm

    http_handler = types.ModuleType("litellm.llms.custom_httpx.http_handler")
    http_handler.AsyncHTTPHandler = type("AsyncHTTPHandler", (), {})
    sys.modules["litellm.llms.custom_httpx.http_handler"] = http_handler
    sys.modules.setdefault("litellm.llms", types.ModuleType("litellm.llms"))
    sys.modules.setdefault("litellm.llms.custom_httpx", types.ModuleType("litellm.llms.custom_httpx"))

    types_mod = types.ModuleType("litellm.types.utils")
    types_mod.ImageObject = ImageObject
    types_mod.ImageResponse = ImageResponse
    sys.modules["litellm.types.utils"] = types_mod
    sys.modules.setdefault("litellm.types", types.ModuleType("litellm.types"))


_ensure_litellm_stubs()

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from custom_handler_seedream import SeedreamException, SeedreamLLM


def _seedream_entries_from_config() -> dict[str, str]:
    """Parse gateway alias → upstream model id from litellm_config.yaml."""
    text = (REPO_ROOT / "litellm_config.yaml").read_text(encoding="utf-8")
    entries: dict[str, str] = {}
    for block in re.finditer(
        r"- model_name: (seedream-5\.0(?:-lite)?)\s+litellm_params:\s+model: seedream/(\S+)",
        text,
    ):
        entries[block.group(1)] = block.group(2)
    return entries


class TestSeedreamConfig(unittest.TestCase):
    def test_litellm_config_maps_gateway_aliases_to_ark_models(self):
        entries = _seedream_entries_from_config()
        self.assertEqual(
            entries,
            {
                GATEWAY_MODEL_5_0: ARK_MODEL_5_0,
                GATEWAY_MODEL_5_0_LITE: ARK_MODEL_5_0_LITE,
            },
        )


class TestSeedreamHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._env = os.environ.get("BYTEDANCE_API_KEY")
        os.environ["BYTEDANCE_API_KEY"] = "test-key"

    def tearDown(self):
        if self._env is None:
            os.environ.pop("BYTEDANCE_API_KEY", None)
        else:
            os.environ["BYTEDANCE_API_KEY"] = self._env

    def _mock_async_client(self, post_resp: MagicMock):
        instance = AsyncMock()
        instance.post = AsyncMock(return_value=post_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        return instance

    async def test_generation_posts_to_modelark_images_generations(self):
        """Handler uses ModelArk id for seedream-5.0-lite gateway alias."""
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(
            return_value={
                "created": 123,
                "data": [{"url": "https://cdn.example/seedream.png"}],
            }
        )

        llm = SeedreamLLM()
        with patch("custom_handler_seedream.httpx.AsyncClient", return_value=self._mock_async_client(response)):
            out = await llm.aimage_generation(
                model=ARK_MODEL_5_0_LITE,
                prompt="A red bicycle",
                model_response=MagicMock(),
                api_key=None,
                api_base=None,
                optional_params={"size": "2K", "watermark": False},
                logging_obj=MagicMock(),
            )

        self.assertEqual(out.data[0].url, "https://cdn.example/seedream.png")
        self.assertAlmostEqual(out._hidden_params["response_cost"], 0.035, places=6)

    async def test_image_urls_mapped_to_image_array(self):
        """Handler uses ModelArk id for seedream-5.0 gateway alias."""
        captured = {}

        async def fake_post(url, headers=None, json=None):
            captured["url"] = url
            captured["json"] = json
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"data": [{"url": "https://cdn.example/out.png"}]})
            return resp

        instance = AsyncMock()
        instance.post = fake_post
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)

        llm = SeedreamLLM()
        with patch("custom_handler_seedream.httpx.AsyncClient", return_value=instance):
            await llm.aimage_generation(
                model=ARK_MODEL_5_0,
                prompt="Edit this product",
                model_response=MagicMock(),
                api_key=None,
                api_base=None,
                optional_params={
                    "image_urls": [
                        "https://example.com/a.png",
                        "https://example.com/b.png",
                    ],
                    "size": "3K",
                },
                logging_obj=MagicMock(),
            )

        self.assertTrue(captured["url"].endswith("/images/generations"))
        self.assertEqual(
            captured["json"]["image"],
            ["https://example.com/a.png", "https://example.com/b.png"],
        )
        self.assertEqual(captured["json"]["model"], ARK_MODEL_5_0)

    async def test_web_search_adds_surcharge(self):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(
            return_value={
                "data": [
                    {"url": "https://cdn.example/1.png"},
                    {"url": "https://cdn.example/2.png"},
                ],
            }
        )

        llm = SeedreamLLM()
        with patch("custom_handler_seedream.httpx.AsyncClient", return_value=self._mock_async_client(response)):
            out = await llm.aimage_generation(
                model=ARK_MODEL_5_0_LITE,
                prompt="Weather infographic for Shanghai",
                model_response=MagicMock(),
                api_key=None,
                api_base=None,
                optional_params={
                    "n": 2,
                    "tools": [{"type": "web_search"}],
                },
                logging_obj=MagicMock(),
            )

        # 2 images × $0.035 + $0.0006 web search
        self.assertAlmostEqual(out._hidden_params["response_cost"], 0.0706, places=6)

    async def test_missing_api_key_raises(self):
        os.environ.pop("BYTEDANCE_API_KEY", None)
        llm = SeedreamLLM()
        with self.assertRaises(ValueError):
            await llm.aimage_generation(
                model=ARK_MODEL_5_0_LITE,
                prompt="test",
                model_response=MagicMock(),
                api_key=None,
                api_base=None,
                optional_params={},
                logging_obj=MagicMock(),
            )

    async def test_http_error_surfaces_seedream_exception(self):
        import httpx

        request = httpx.Request("POST", "https://ark.example/images/generations")
        response = httpx.Response(400, request=request, json={"error": {"message": "bad prompt"}})
        err = httpx.HTTPStatusError("bad", request=request, response=response)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock(side_effect=err)
        mock_resp.json = MagicMock(return_value={"error": {"message": "bad prompt"}})

        instance = AsyncMock()
        instance.post = AsyncMock(return_value=mock_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)

        llm = SeedreamLLM()
        with patch("custom_handler_seedream.httpx.AsyncClient", return_value=instance):
            with self.assertRaises(SeedreamException) as ctx:
                await llm.aimage_generation(
                    model=ARK_MODEL_5_0_LITE,
                    prompt="x",
                    model_response=MagicMock(),
                    api_key=None,
                    api_base=None,
                    optional_params={},
                    logging_obj=MagicMock(),
                )
        self.assertIn("bad prompt", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

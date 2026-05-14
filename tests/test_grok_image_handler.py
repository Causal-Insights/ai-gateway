"""
Tests for Grok image custom handler.

Uses unittest + mocks only. Stubs ``litellm`` so CI/local runs need not install it.
"""

import io
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def _ensure_litellm_stubs():
    if "litellm" in sys.modules:
        return

    class CustomLLM:  # noqa: D401
        """LiteLLM base (stub)."""

    class ImageObject:
        def __init__(self, url=None, **kwargs):
            self.url = url

    class ImageResponse:
        def __init__(self, created=0, data=None, **kwargs):
            self.created = created
            self.data = data if data is not None else []

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

# Import after stubs (repo root on path when running ``python -m unittest`` from parent)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_handler import GrokImageException, GrokImageLLM


class TestGrokImageHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._env = os.environ.get("GROK_API_KEY")
        os.environ["GROK_API_KEY"] = "test-key"

    def tearDown(self):
        if self._env is None:
            os.environ.pop("GROK_API_KEY", None)
        else:
            os.environ["GROK_API_KEY"] = self._env

    def _mock_async_client(self, post_resp: MagicMock):
        instance = AsyncMock()
        instance.post = AsyncMock(return_value=post_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        return instance

    async def test_generation_routes_to_images_generations(self):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(
            return_value={
                "data": [
                    {"url": "https://cdn.example/generated.jpg"},
                ]
            }
        )

        client_instance = self._mock_async_client(response)

        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance):
            llm = GrokImageLLM()
            out = await llm.aimage_generation(
                model="grok-image/grok-imagine-image-quality",
                prompt="A watercolor fox in a forest",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={},
                logging_obj=None,
            )

        self.assertEqual(len(out.data), 1)
        self.assertEqual(out.data[0].url, "https://cdn.example/generated.jpg")
        submit_url = client_instance.post.call_args.args[0]
        submit_body = client_instance.post.call_args.kwargs["json"]
        self.assertEqual(submit_url, "https://api.x.ai/v1/images/generations")
        self.assertEqual(submit_body["model"], "grok-imagine-image-quality")

    async def test_image_url_routes_to_images_edits(self):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(
            return_value={
                "data": [
                    {"url": "https://cdn.example/edited.jpg"},
                ]
            }
        )

        client_instance = self._mock_async_client(response)

        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance):
            llm = GrokImageLLM()
            out = await llm.aimage_generation(
                model="grok-image/grok-imagine-image-quality",
                prompt="Make it black and white",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"image_url": "https://cdn.example/input.png"},
                logging_obj=None,
            )

        self.assertEqual(len(out.data), 1)
        self.assertEqual(out.data[0].url, "https://cdn.example/edited.jpg")
        submit_url = client_instance.post.call_args.args[0]
        submit_body = client_instance.post.call_args.kwargs["json"]
        self.assertEqual(submit_url, "https://api.x.ai/v1/images/edits")
        self.assertEqual(submit_body["image"]["url"], "https://cdn.example/input.png")
        self.assertEqual(submit_body["image"]["type"], "image_url")

    async def test_aimage_edit_routes_to_images_edits(self):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(
            return_value={
                "data": [
                    {"url": "https://cdn.example/edited-explicit.jpg"},
                ]
            }
        )

        client_instance = self._mock_async_client(response)

        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance):
            llm = GrokImageLLM()
            out = await llm.aimage_edit(
                model="grok-image/grok-imagine-image-quality",
                image=["https://cdn.example/edit-input.png"],
                prompt="Add dramatic sunset lighting",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={},
                logging_obj=None,
            )

        self.assertEqual(len(out.data), 1)
        self.assertEqual(out.data[0].url, "https://cdn.example/edited-explicit.jpg")
        submit_url = client_instance.post.call_args.args[0]
        submit_body = client_instance.post.call_args.kwargs["json"]
        self.assertEqual(submit_url, "https://api.x.ai/v1/images/edits")
        self.assertEqual(submit_body["image"]["url"], "https://cdn.example/edit-input.png")
        self.assertEqual(submit_body["image"]["type"], "image_url")

    async def test_aimage_edit_bytesio_becomes_data_uri_json(self):
        """LiteLLM proxy passes multipart uploads as BytesIO; xAI expects JSON + data URI."""
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(
            return_value={
                "data": [
                    {"url": "https://cdn.example/edited-from-bytesio.jpg"},
                ]
            }
        )

        client_instance = self._mock_async_client(response)

        upload = io.BytesIO(b"\x89PNG\r\n\x1a\nfake")
        upload.name = "source.png"

        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance):
            llm = GrokImageLLM()
            out = await llm.aimage_edit(
                model="grok-image/grok-imagine-image-quality",
                image=[upload],
                prompt="Make background blue",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={},
                logging_obj=None,
            )

        self.assertEqual(len(out.data), 1)
        self.assertEqual(out.data[0].url, "https://cdn.example/edited-from-bytesio.jpg")
        submit_url = client_instance.post.call_args.args[0]
        submit_body = client_instance.post.call_args.kwargs["json"]
        self.assertEqual(submit_url, "https://api.x.ai/v1/images/edits")
        self.assertEqual(submit_body["image"]["type"], "image_url")
        self.assertTrue(submit_body["image"]["url"].startswith("data:image/png;base64,"))

    def test_image_edit_sync_bytesio(self):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(
            return_value={"data": [{"url": "https://cdn.example/sync-edit.jpg"}]}
        )

        client_instance = MagicMock()
        client_instance.post = MagicMock(return_value=response)
        client_instance.__enter__ = MagicMock(return_value=client_instance)
        client_instance.__exit__ = MagicMock(return_value=None)

        upload = io.BytesIO(b"pretend-jpeg")
        upload.name = "x.jpg"

        with patch("custom_handler_xai.httpx.Client", return_value=client_instance):
            llm = GrokImageLLM()
            out = llm.image_edit(
                model="grok-image/grok-imagine-image-quality",
                image=[upload],
                prompt="Warm color grade",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={},
                logging_obj=None,
            )

        self.assertEqual(out.data[0].url, "https://cdn.example/sync-edit.jpg")
        submit_body = client_instance.post.call_args.kwargs["json"]
        self.assertTrue(submit_body["image"]["url"].startswith("data:image/jpeg;base64,"))

    async def test_failed_http_raises_grok_image_exception(self):
        req = MagicMock()
        resp = MagicMock()
        resp.status_code = 500
        resp.json = MagicMock(return_value={"error": {"message": "internal error"}})
        error = __import__("httpx").HTTPStatusError("500", request=req, response=resp)

        response = MagicMock()
        response.raise_for_status = MagicMock(side_effect=error)
        response.json = MagicMock(return_value={"error": {"message": "internal error"}})

        client_instance = self._mock_async_client(response)

        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance):
            llm = GrokImageLLM()
            with self.assertRaises(GrokImageException) as ctx:
                await llm.aimage_generation(
                    model="grok-image/grok-imagine-image-quality",
                    prompt="A mountain",
                    model_response=None,
                    api_key=None,
                    api_base=None,
                    optional_params={},
                    logging_obj=None,
                )

        self.assertIn("internal error", str(ctx.exception))

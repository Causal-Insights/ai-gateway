"""
Tests for Grok image custom handler.

Uses unittest + mocks only. Stubs ``litellm`` so CI/local runs need not install it.
"""

import base64
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
            for key, value in kwargs.items():
                setattr(self, key, value)

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

    async def test_image_2_generation_preserves_model_quality_and_fallback_cost(self):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(
            return_value={"data": [{"b64_json": "ZmFrZS1pbWFnZQ==", "mime_type": "image/jpeg"}]}
        )
        client_instance = self._mock_async_client(response)

        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance):
            out = await GrokImageLLM().aimage_generation(
                model="grok-image/grok-imagine-image-2.0",
                prompt="A precise concert poster with sharp small print",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"resolution": "2K", "quality": "medium", "response_format": "b64_json"},
                logging_obj=None,
            )

        submit_body = client_instance.post.call_args.kwargs["json"]
        self.assertEqual(submit_body["model"], "grok-imagine-image-2.0")
        self.assertEqual(submit_body["resolution"], "2k")
        self.assertEqual(submit_body["quality"], "medium")
        self.assertEqual(submit_body["response_format"], "b64_json")
        self.assertEqual(out.data[0].b64_json, "ZmFrZS1pbWFnZQ==")
        self.assertAlmostEqual(out._hidden_params["response_cost"], 0.08)

    async def test_image_2_restores_request_policy_private_fields(self):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value={"data": [{"b64_json": "ZmFrZQ=="}]})
        client_instance = self._mock_async_client(response)

        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance):
            out = await GrokImageLLM().aimage_generation(
                model="grok-image/grok-imagine-image-2.0",
                prompt="A low-cost draft",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={
                    "resolution": "1K",
                    "xai_output_count": 1,
                    "xai_render_quality": "low",
                    "xai_response_format": "b64_json",
                },
                logging_obj=None,
            )

        submit_body = client_instance.post.call_args.kwargs["json"]
        self.assertEqual(submit_body["n"], 1)
        self.assertEqual(submit_body["quality"], "low")
        self.assertEqual(submit_body["response_format"], "b64_json")
        self.assertAlmostEqual(out._hidden_params["response_cost"], 0.04)

    async def test_image_2_materializes_ignored_base64_request_from_temporary_url(self):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value={"data": [{"url": "https://imgen.x.ai/temporary.jpg"}]})
        download = MagicMock()
        download.raise_for_status = MagicMock()
        download.content = b"generated-image"
        download.headers = {"content-type": "image/jpeg"}
        client_instance = self._mock_async_client(response)
        client_instance.get = AsyncMock(return_value=download)

        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance):
            out = await GrokImageLLM().aimage_generation(
                model="grok-image/grok-imagine-image-2.0",
                prompt="Return base64",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"response_format": "b64_json"},
                logging_obj=None,
            )

        client_instance.get.assert_awaited_once_with("https://imgen.x.ai/temporary.jpg")
        self.assertEqual(out.data[0].b64_json, base64.standard_b64encode(b"generated-image").decode("ascii"))
        self.assertIsNone(out.data[0].url)

    async def test_image_2_accepts_five_inputs_and_rejects_six(self):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value={"data": [{"url": "https://cdn.example/edited.jpg"}]})
        client_instance = self._mock_async_client(response)
        images = [f"https://cdn.example/{index}.png" for index in range(5)]

        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance):
            out = await GrokImageLLM().aimage_edit(
                model="grok-image/grok-imagine-image-2.0",
                image=images,
                prompt="Combine all five references",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"quality": "low"},
                logging_obj=None,
            )
        self.assertEqual(len(client_instance.post.call_args.kwargs["json"]["images"]), 5)
        self.assertAlmostEqual(out._hidden_params["response_cost"], 0.09)

        with self.assertRaisesRegex(ValueError, "one to 5 images"):
            await GrokImageLLM().aimage_edit(
                model="grok-image/grok-imagine-image-2.0",
                image=images + ["https://cdn.example/extra.png"],
                prompt="Too many references",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={},
                logging_obj=None,
            )

    async def test_image_2_rejects_invalid_quality_before_upstream_call(self):
        with self.assertRaisesRegex(ValueError, "quality must be low or medium"):
            await GrokImageLLM().aimage_generation(
                model="grok-image/grok-imagine-image-2.0",
                prompt="Invalid quality",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"quality": "high"},
                logging_obj=None,
            )

    async def test_image_2_rejects_unnormalized_openai_size_before_upstream_call(self):
        with self.assertRaisesRegex(ValueError, "normalized to xAI aspect_ratio"):
            await GrokImageLLM().aimage_generation(
                model="grok-image/grok-imagine-image-2.0",
                prompt="A square poster",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"size": "1024x1024"},
                logging_obj=None,
            )

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

    async def test_multi_image_edit_preserves_model_and_exact_usage_cost(self):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(
            return_value={
                "data": [
                    {
                        "b64_json": "ZmFrZS1pbWFnZQ==",
                        "mime_type": "image/jpeg",
                        "revised_prompt": "revised",
                        "file_output": {"file_id": "file_123"},
                    }
                ],
                "usage": {"cost_in_usd_ticks": 900_000_000},
            }
        )
        client_instance = self._mock_async_client(response)
        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance):
            out = await GrokImageLLM().aimage_edit(
                model="grok-image/future-official-model",
                image=[
                    "https://cdn.example/one.png",
                    {"file_id": "file_two"},
                    io.BytesIO(b"third"),
                ],
                prompt="Combine them",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"resolution": "2K", "storage_options": {"filename": "out.jpg"}},
                logging_obj=None,
            )
        payload = client_instance.post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "future-official-model")
        self.assertEqual(len(payload["images"]), 3)
        self.assertEqual(payload["resolution"], "2k")
        self.assertEqual(out.data[0].b64_json, "ZmFrZS1pbWFnZQ==")
        self.assertEqual(out.data[0].revised_prompt, "revised")
        self.assertAlmostEqual(out._hidden_params["response_cost"], 0.09)

    async def test_two_k_fallback_cost_counts_inputs_and_outputs(self):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(
            return_value={"data": [{"url": "https://cdn.example/one.jpg"}, {"url": "https://cdn.example/two.jpg"}]}
        )
        client_instance = self._mock_async_client(response)
        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance):
            out = await GrokImageLLM().aimage_edit(
                model="grok-image/grok-imagine-image-quality",
                image=["https://cdn.example/input.png"],
                prompt="Two variants",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"size": "2K", "n": 2},
                logging_obj=None,
            )
        self.assertEqual(client_instance.post.call_args.kwargs["json"]["resolution"], "2k")
        self.assertAlmostEqual(out._hidden_params["response_cost"], 0.15)

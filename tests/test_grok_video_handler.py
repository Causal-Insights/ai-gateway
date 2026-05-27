"""
Tests for Grok video custom handler.

Uses unittest + mocks only. Stubs ``litellm`` so CI/local runs need not install it.
"""

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

# Import after stubs (repo root on path when running ``python -m unittest`` from parent)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from custom_handler import GrokVideoException, GrokVideoLLM


class TestGrokVideoHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._env = os.environ.get("GROK_API_KEY")
        os.environ["GROK_API_KEY"] = "test-key"

    def tearDown(self):
        if self._env is None:
            os.environ.pop("GROK_API_KEY", None)
        else:
            os.environ["GROK_API_KEY"] = self._env

    def _mock_async_client(self, post_resp: MagicMock, get_responses: list):
        instance = AsyncMock()
        instance.post = AsyncMock(return_value=post_resp)
        instance.get = AsyncMock(side_effect=get_responses)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        return instance

    async def test_done_returns_video_url(self):
        submit_resp = MagicMock()
        submit_resp.raise_for_status = MagicMock()
        submit_resp.json = MagicMock(return_value={"request_id": "req-ok"})

        done_resp = MagicMock()
        done_resp.raise_for_status = MagicMock()
        done_resp.json = MagicMock(
            return_value={
                "status": "done",
                "video": {"url": "https://cdn.example/grok.mp4"},
            }
        )

        client_instance = self._mock_async_client(submit_resp, [done_resp])

        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance), patch(
            "custom_handler_xai.asyncio.sleep", new=AsyncMock(return_value=None)
        ):
            llm = GrokVideoLLM()
            out = await llm.aimage_generation(
                model="grok-video/grok-2-video-generation",
                prompt="hello",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"duration": 1},
                logging_obj=None,
            )

        self.assertEqual(len(out.data), 1)
        self.assertEqual(out.data[0].url, "https://cdn.example/grok.mp4")

    async def test_done_sets_response_cost_from_usd_ticks(self):
        submit_resp = MagicMock()
        submit_resp.raise_for_status = MagicMock()
        submit_resp.json = MagicMock(return_value={"request_id": "req-cost"})

        done_resp = MagicMock()
        done_resp.raise_for_status = MagicMock()
        done_resp.json = MagicMock(
            return_value={
                "status": "done",
                "video": {"url": "https://cdn.example/grok.mp4", "duration": 5},
                "usage": {"cost_in_usd_ticks": 350_000_000},
            }
        )

        client_instance = self._mock_async_client(submit_resp, [done_resp])

        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance), patch(
            "custom_handler_xai.asyncio.sleep", new=AsyncMock(return_value=None)
        ):
            llm = GrokVideoLLM()
            out = await llm.aimage_generation(
                model="grok-video/grok-imagine-video",
                prompt="hello",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"duration": 5, "resolution": "720p"},
                logging_obj=None,
            )

        self.assertAlmostEqual(out._hidden_params["response_cost"], 0.035, places=6)

    async def test_done_estimates_cost_when_ticks_missing(self):
        submit_resp = MagicMock()
        submit_resp.raise_for_status = MagicMock()
        submit_resp.json = MagicMock(return_value={"request_id": "req-est"})

        done_resp = MagicMock()
        done_resp.raise_for_status = MagicMock()
        done_resp.json = MagicMock(
            return_value={
                "status": "done",
                "video": {"url": "https://cdn.example/grok.mp4", "duration": 4},
            }
        )

        client_instance = self._mock_async_client(submit_resp, [done_resp])

        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance), patch(
            "custom_handler_xai.asyncio.sleep", new=AsyncMock(return_value=None)
        ):
            llm = GrokVideoLLM()
            out = await llm.aimage_generation(
                model="grok-video/grok-imagine-video",
                prompt="hello",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"resolution": "720p"},
                logging_obj=None,
            )

        self.assertAlmostEqual(out._hidden_params["response_cost"], 0.28, places=6)

    async def test_failed_internal_error_raises_grok_exception(self):
        submit_resp = MagicMock()
        submit_resp.raise_for_status = MagicMock()
        submit_resp.json = MagicMock(return_value={"request_id": "req-fail"})

        failed_resp = MagicMock()
        failed_resp.raise_for_status = MagicMock()
        failed_resp.json = MagicMock(
            return_value={
                "status": "failed",
                "error": {
                    "code": "internal_error",
                    "message": "Video generation failed due to an internal error. Please try again.",
                },
                "video": {"url": "", "duration": 0},
            }
        )

        client_instance = self._mock_async_client(submit_resp, [failed_resp])

        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance), patch(
            "custom_handler_xai.asyncio.sleep", new=AsyncMock(return_value=None)
        ):
            llm = GrokVideoLLM()
            with self.assertRaises(GrokVideoException) as ctx:
                await llm.aimage_generation(
                    model="grok-video/grok-2-video-generation",
                    prompt="hello",
                    model_response=None,
                    api_key=None,
                    api_base=None,
                    optional_params={"duration": 1},
                    logging_obj=None,
                )

        self.assertIn("internal_error", str(ctx.exception))
        self.assertIn("Please try again", str(ctx.exception))

    async def test_video_url_routes_to_edits_endpoint(self):
        submit_resp = MagicMock()
        submit_resp.raise_for_status = MagicMock()
        submit_resp.json = MagicMock(return_value={"request_id": "req-edit"})

        done_resp = MagicMock()
        done_resp.raise_for_status = MagicMock()
        done_resp.json = MagicMock(
            return_value={
                "status": "done",
                "video": {"url": "https://cdn.example/edit.mp4"},
            }
        )

        client_instance = self._mock_async_client(submit_resp, [done_resp])

        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance), patch(
            "custom_handler_xai.asyncio.sleep", new=AsyncMock(return_value=None)
        ):
            llm = GrokVideoLLM()
            out = await llm.aimage_generation(
                model="grok-video/grok-2-video-generation",
                prompt="Make the scene brighter",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"video_url": "https://cdn.example/input.mp4"},
                logging_obj=None,
            )

        self.assertEqual(len(out.data), 1)
        self.assertEqual(out.data[0].url, "https://cdn.example/edit.mp4")
        submit_url = client_instance.post.call_args.args[0]
        submit_body = client_instance.post.call_args.kwargs["json"]
        self.assertEqual(submit_url, "https://api.x.ai/v1/videos/edits")
        self.assertEqual(submit_body["video"], {"url": "https://cdn.example/input.mp4"})
        self.assertEqual(submit_body["prompt"], "Make the scene brighter")

    async def test_generation_seconds_alias_and_image_url_alias(self):
        submit_resp = MagicMock()
        submit_resp.raise_for_status = MagicMock()
        submit_resp.json = MagicMock(return_value={"request_id": "req-gen"})

        done_resp = MagicMock()
        done_resp.raise_for_status = MagicMock()
        done_resp.json = MagicMock(
            return_value={
                "status": "done",
                "video": {"url": "https://cdn.example/gen.mp4"},
            }
        )

        client_instance = self._mock_async_client(submit_resp, [done_resp])

        with patch("custom_handler_xai.httpx.AsyncClient", return_value=client_instance), patch(
            "custom_handler_xai.asyncio.sleep", new=AsyncMock(return_value=None)
        ):
            llm = GrokVideoLLM()
            await llm.aimage_generation(
                model="grok-video/grok-2-video-generation",
                prompt="",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={
                    "seconds": "8",
                    "image_url": "https://cdn.example/image.png",
                },
                logging_obj=None,
            )

        submit_url = client_instance.post.call_args.args[0]
        submit_body = client_instance.post.call_args.kwargs["json"]
        self.assertEqual(submit_url, "https://api.x.ai/v1/videos/generations")
        self.assertEqual(submit_body["duration"], 8)
        self.assertEqual(submit_body["image"], {"url": "https://cdn.example/image.png"})
        self.assertNotIn("prompt", submit_body)

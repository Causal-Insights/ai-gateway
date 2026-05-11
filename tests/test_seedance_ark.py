"""
Tests for Seedance 2.0 custom handler (BytePlus ARK).

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

from custom_handler import DEFAULT_ARK_BASE, SeedanceException, SeedanceLLM, normalize_error


class TestNormalizeError(unittest.TestCase):
    def test_strip_and_fallback(self):
        self.assertEqual(normalize_error("  x  "), "x")
        self.assertEqual(normalize_error(None), "unknown error")
        self.assertEqual(normalize_error(""), "unknown error")


class TestSeedanceARK(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._env = os.environ.get("BYTEDANCE_API_KEY")
        os.environ["BYTEDANCE_API_KEY"] = "test-key"
        os.environ["SEEDANCE_POLL_INTERVAL_S"] = "0"
        os.environ["SEEDANCE_POLL_TIMEOUT_S"] = "60"

    def tearDown(self):
        if self._env is None:
            os.environ.pop("BYTEDANCE_API_KEY", None)
        else:
            os.environ["BYTEDANCE_API_KEY"] = self._env
        os.environ.pop("SEEDANCE_POLL_INTERVAL_S", None)
        os.environ.pop("SEEDANCE_POLL_TIMEOUT_S", None)

    def _mock_async_client(self, post_resp: MagicMock, get_responses: list):
        instance = AsyncMock()
        instance.post = AsyncMock(return_value=post_resp)
        instance.get = AsyncMock(side_effect=get_responses)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        return instance

    async def test_submit_url_and_body(self):
        submit_resp = MagicMock()
        submit_resp.raise_for_status = MagicMock()
        submit_resp.json = MagicMock(return_value={"id": "task-abc"})

        poll_ok = MagicMock()
        poll_ok.raise_for_status = MagicMock()
        poll_ok.json = MagicMock(
            return_value={
                "status": "succeeded",
                "content": {"video_url": "https://cdn.example/out.mp4"},
            }
        )

        client_instance = self._mock_async_client(submit_resp, [poll_ok])

        with patch("custom_handler.httpx.AsyncClient", return_value=client_instance):
            llm = SeedanceLLM()
            await llm.aimage_generation(
                model="dreamina-seedance-2-0-260128",
                prompt="hello",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={
                    "resolution": "480p",
                    "ratio": "16:9",
                    "duration": 4,
                    "generate_audio": False,
                    "watermark": True,
                },
                logging_obj=None,
            )

        client_instance.post.assert_called_once()
        args, kwargs = client_instance.post.call_args
        url = args[0]
        self.assertEqual(url, f"{DEFAULT_ARK_BASE}/contents/generations/tasks")
        body = kwargs["json"]
        self.assertEqual(body["model"], "dreamina-seedance-2-0-260128")
        self.assertEqual(body["content"][0], {"type": "text", "text": "hello"})
        self.assertEqual(body["resolution"], "480p")
        self.assertEqual(body["ratio"], "16:9")
        self.assertEqual(body["duration"], 4)
        self.assertIs(body["generate_audio"], False)
        self.assertIs(body["watermark"], True)

    async def test_submit_appends_image_content_parts(self):
        submit_resp = MagicMock()
        submit_resp.raise_for_status = MagicMock()
        submit_resp.json = MagicMock(return_value={"id": "task-img"})

        poll_ok = MagicMock()
        poll_ok.raise_for_status = MagicMock()
        poll_ok.json = MagicMock(
            return_value={"status": "succeeded", "content": {"video_url": "https://x/v.mp4"}}
        )

        client_instance = self._mock_async_client(submit_resp, [poll_ok])

        with patch("custom_handler.httpx.AsyncClient", return_value=client_instance):
            llm = SeedanceLLM()
            await llm.aimage_generation(
                model="dreamina-seedance-2-0-260128",
                prompt="animate this",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={
                    "image": "https://cdn.example/a.png",
                    "images": ["https://cdn.example/b.png"],
                },
                logging_obj=None,
            )

        body = client_instance.post.call_args.kwargs["json"]
        self.assertEqual(
            body["content"],
            [
                {"type": "text", "text": "animate this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://cdn.example/a.png"},
                    "role": "first_frame",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": "https://cdn.example/b.png"},
                    "role": "last_frame",
                },
            ],
        )

    async def test_single_image_uses_first_frame_role(self):
        submit_resp = MagicMock()
        submit_resp.raise_for_status = MagicMock()
        submit_resp.json = MagicMock(return_value={"id": "task-one-img"})

        poll_ok = MagicMock()
        poll_ok.raise_for_status = MagicMock()
        poll_ok.json = MagicMock(
            return_value={"status": "succeeded", "content": {"video_url": "https://x/v.mp4"}}
        )

        client_instance = self._mock_async_client(submit_resp, [poll_ok])

        with patch("custom_handler.httpx.AsyncClient", return_value=client_instance):
            llm = SeedanceLLM()
            await llm.aimage_generation(
                model="dreamina-seedance-2-0-260128",
                prompt="motion",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"image": "https://cdn.example/one.png"},
                logging_obj=None,
            )

        body = client_instance.post.call_args.kwargs["json"]["content"]
        self.assertEqual(len(body), 2)
        self.assertEqual(body[1]["role"], "first_frame")

    async def test_reference_image_urls_use_reference_image_role(self):
        submit_resp = MagicMock()
        submit_resp.raise_for_status = MagicMock()
        submit_resp.json = MagicMock(return_value={"id": "task-ref"})

        poll_ok = MagicMock()
        poll_ok.raise_for_status = MagicMock()
        poll_ok.json = MagicMock(
            return_value={"status": "succeeded", "content": {"video_url": "https://x/v.mp4"}}
        )

        client_instance = self._mock_async_client(submit_resp, [poll_ok])

        with patch("custom_handler.httpx.AsyncClient", return_value=client_instance):
            llm = SeedanceLLM()
            await llm.aimage_generation(
                model="dreamina-seedance-2-0-260128",
                prompt="use @image1 style",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={
                    "reference_image_urls": [
                        "https://cdn.example/r1.png",
                        "https://cdn.example/r2.png",
                    ],
                },
                logging_obj=None,
            )

        body = client_instance.post.call_args.kwargs["json"]["content"]
        self.assertEqual(body[1]["role"], "reference_image")
        self.assertEqual(body[2]["role"], "reference_image")

    async def test_poll_running_then_succeeded_image_response(self):
        submit_resp = MagicMock()
        submit_resp.raise_for_status = MagicMock()
        submit_resp.json = MagicMock(return_value={"id": "task-xyz"})

        running = MagicMock()
        running.raise_for_status = MagicMock()
        running.json = MagicMock(return_value={"status": "running"})

        succeeded = MagicMock()
        succeeded.raise_for_status = MagicMock()
        succeeded.json = MagicMock(
            return_value={
                "status": "succeeded",
                "content": {"video_url": "https://cdn.example/v.mp4"},
            }
        )

        client_instance = self._mock_async_client(submit_resp, [running, running, succeeded])

        with patch("custom_handler.httpx.AsyncClient", return_value=client_instance):
            llm = SeedanceLLM()
            out = await llm.aimage_generation(
                model="dreamina-seedance-2-0-260128",
                prompt="p",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={},
                logging_obj=None,
            )

        self.assertEqual(len(out.data), 1)
        self.assertEqual(out.data[0].url, "https://cdn.example/v.mp4")

    async def test_failed_raises_seedance_exception(self):
        submit_resp = MagicMock()
        submit_resp.raise_for_status = MagicMock()
        submit_resp.json = MagicMock(return_value={"id": "task-fail"})

        failed = MagicMock()
        failed.raise_for_status = MagicMock()
        failed.json = MagicMock(
            return_value={
                "status": "failed",
                "error": {"message": "quota exceeded"},
            }
        )

        client_instance = self._mock_async_client(submit_resp, [failed])

        with patch("custom_handler.httpx.AsyncClient", return_value=client_instance):
            llm = SeedanceLLM()
            with self.assertRaises(SeedanceException) as ctx:
                await llm.aimage_generation(
                    model="dreamina-seedance-2-0-260128",
                    prompt="p",
                    model_response=None,
                    api_key=None,
                    api_base=None,
                    optional_params={},
                    logging_obj=None,
                )
        self.assertEqual(str(ctx.exception), "quota exceeded")

    def test_no_legacy_visual_api_strings_in_other_tests(self):
        """Guardrail: legacy visual CV submit/poll paths must not reappear in other tests."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        banned = (
            "volc" + "engineapi.com",
            "Action=" + "CV" + "Process",
            "CVSync" + "2" + "Async",
        )
        offenders = []
        for dirpath, _, filenames in os.walk(os.path.join(root, "tests")):
            for name in filenames:
                if not name.endswith(".py") or name == "test_seedance_ark.py":
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                for b in banned:
                    if b.lower() in text.lower():
                        offenders.append((path, b))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()

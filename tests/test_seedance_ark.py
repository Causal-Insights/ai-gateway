"""
Tests for Seedance 2.0 custom handler (BytePlus ARK).

Uses unittest + mocks only. Stubs ``litellm`` so CI/local runs need not install it.
"""

import json
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
        def __init__(self, url=None, revised_prompt=None, b64_json=None, **kwargs):
            self.url = url
            self.revised_prompt = revised_prompt
            self.b64_json = b64_json

    class ImageResponse:
        def __init__(self, created=0, data=None, **kwargs):
            self.created = created
            self.data = data if data is not None else []
            self._hidden_params = {}
            for key, value in kwargs.items():
                setattr(self, key, value)

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

from custom_handler import (
    DEFAULT_ARK_BASE,
    SeedanceException,
    SeedanceLLM,
    normalize_error,
)
from custom_handler_seedance import TASK_URL_SCHEME


class TestNormalizeError(unittest.TestCase):
    def test_strip_and_fallback(self):
        self.assertEqual(normalize_error("  x  "), "x")
        self.assertEqual(normalize_error(None), "unknown error")
        self.assertEqual(normalize_error(""), "unknown error")


class TestSeedanceARK(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._env_snapshot = {
            k: os.environ.get(k)
            for k in (
                "BYTEDANCE_API_KEY",
                "SEEDANCE_POLL_INTERVAL_S",
                "SEEDANCE_POLL_TIMEOUT_S",
                "SEEDANCE_SYNC_WAIT_S",
                "SEEDANCE_BLOCKING_POLL",
            )
        }
        os.environ["BYTEDANCE_API_KEY"] = "test-key"
        os.environ["SEEDANCE_POLL_INTERVAL_S"] = "0"
        os.environ["SEEDANCE_POLL_TIMEOUT_S"] = "60"
        os.environ["SEEDANCE_SYNC_WAIT_S"] = "30"
        os.environ.pop("SEEDANCE_BLOCKING_POLL", None)

    def tearDown(self):
        for key, value in self._env_snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _mock_async_client(self, post_resp, get_responses):
        instance = AsyncMock()
        instance.post = AsyncMock(return_value=post_resp) if post_resp is not None else AsyncMock()
        instance.get = AsyncMock(side_effect=get_responses)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=None)
        return instance

    @staticmethod
    def _resp(json_value):
        m = MagicMock()
        m.raise_for_status = MagicMock()
        m.json = MagicMock(return_value=json_value)
        return m

    # --- submit + first-poll happy path --------------------------------------

    async def test_submit_url_and_body(self):
        submit = self._resp({"id": "task-abc"})
        succeeded = self._resp(
            {
                "status": "succeeded",
                "model": "dreamina-seedance-2-0-260128",
                "usage": {"completion_tokens": 100_000, "total_tokens": 100_000},
                "content": {"video_url": "https://cdn.example/out.mp4"},
            }
        )
        client_instance = self._mock_async_client(submit, [succeeded])

        with patch("custom_handler_seedance.httpx.AsyncClient", return_value=client_instance):
            llm = SeedanceLLM()
            out = await llm.aimage_generation(
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
        self.assertEqual(args[0], f"{DEFAULT_ARK_BASE}/contents/generations/tasks")
        body = kwargs["json"]
        self.assertEqual(body["model"], "dreamina-seedance-2-0-260128")
        self.assertEqual(body["content"][0], {"type": "text", "text": "hello"})
        self.assertEqual(body["resolution"], "480p")
        self.assertEqual(body["ratio"], "16:9")
        self.assertEqual(body["duration"], 4)
        self.assertIs(body["generate_audio"], False)
        self.assertIs(body["watermark"], True)

        self.assertEqual(out.data[0].url, "https://cdn.example/out.mp4")
        self.assertAlmostEqual(out._hidden_params["response_cost"], 100_000 * 7.00 / 1_000_000)

    async def test_submit_appends_image_content_parts(self):
        submit = self._resp({"id": "task-img"})
        succeeded = self._resp(
            {"status": "succeeded", "content": {"video_url": "https://x/v.mp4"}}
        )
        client_instance = self._mock_async_client(submit, [succeeded])

        with patch("custom_handler_seedance.httpx.AsyncClient", return_value=client_instance):
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
        submit = self._resp({"id": "task-one-img"})
        succeeded = self._resp(
            {"status": "succeeded", "content": {"video_url": "https://x/v.mp4"}}
        )
        client_instance = self._mock_async_client(submit, [succeeded])

        with patch("custom_handler_seedance.httpx.AsyncClient", return_value=client_instance):
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
        submit = self._resp({"id": "task-ref"})
        succeeded = self._resp(
            {"status": "succeeded", "content": {"video_url": "https://x/v.mp4"}}
        )
        client_instance = self._mock_async_client(submit, [succeeded])

        with patch("custom_handler_seedance.httpx.AsyncClient", return_value=client_instance):
            llm = SeedanceLLM()
            await llm.aimage_generation(
                model="dreamina-seedance-2-0-260128",
                prompt="use image1 style",
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

    async def test_video_url_appended_as_reference_video(self):
        submit = self._resp({"id": "task-vid"})
        succeeded = self._resp(
            {
                "status": "succeeded",
                "usage": {"completion_tokens": 50_000},
                "content": {"video_url": "https://x/edited.mp4"},
            }
        )
        client_instance = self._mock_async_client(submit, [succeeded])

        with patch("custom_handler_seedance.httpx.AsyncClient", return_value=client_instance):
            llm = SeedanceLLM()
            out = await llm.aimage_generation(
                model="dreamina-seedance-2-0-260128",
                prompt="replace cat with lion",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"video_url": "https://cdn.example/src.mp4"},
                logging_obj=None,
            )

        body = client_instance.post.call_args.kwargs["json"]["content"]
        self.assertEqual(body[-1]["type"], "video_url")
        self.assertEqual(body[-1]["role"], "reference_video")
        # has_input_video=True -> uses lower rate ($4.30/M)
        self.assertAlmostEqual(out._hidden_params["response_cost"], 50_000 * 4.30 / 1_000_000)

    async def test_running_then_succeeded_during_sync_wait(self):
        submit = self._resp({"id": "task-xyz"})
        running = self._resp({"status": "running"})
        succeeded = self._resp(
            {
                "status": "succeeded",
                "usage": {"completion_tokens": 90_000},
                "content": {"video_url": "https://cdn.example/v.mp4"},
            }
        )
        client_instance = self._mock_async_client(submit, [running, running, succeeded])

        with patch("custom_handler_seedance.httpx.AsyncClient", return_value=client_instance):
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

        self.assertEqual(out.data[0].url, "https://cdn.example/v.mp4")
        self.assertEqual(out.data[0].revised_prompt, "task-xyz")

    async def test_submit_appends_task_ledger(self):
        submit = self._resp({"id": "task-ledger"})
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            ledger_path = tmp.name

        try:
            with patch.dict(
                os.environ,
                {"SEEDANCE_TASK_LEDGER_PATH": ledger_path, "SEEDANCE_SYNC_WAIT_S": "0"},
            ):
                client_instance = self._mock_async_client(submit, [])
                with patch("custom_handler_seedance.httpx.AsyncClient", return_value=client_instance):
                    await SeedanceLLM().aimage_generation(
                        model="dreamina-seedance-2-0-260128",
                        prompt="ledger test prompt",
                        model_response=None,
                        api_key=None,
                        api_base=None,
                        optional_params={},
                        logging_obj=None,
                    )

            with open(ledger_path, encoding="utf-8") as f:
                row = json.loads(f.readline())

            self.assertEqual(row["task_id"], "task-ledger")
            self.assertEqual(row["resolution"], "480p")
            self.assertIn("ledger test", row["prompt_preview"])
        finally:
            os.unlink(ledger_path)

    async def test_failed_raises_seedance_exception(self):
        submit = self._resp({"id": "task-fail"})
        failed = self._resp({"status": "failed", "error": {"message": "quota exceeded"}})
        client_instance = self._mock_async_client(submit, [failed])

        with patch("custom_handler_seedance.httpx.AsyncClient", return_value=client_instance):
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

    # --- bounded-wait fallback to async response -----------------------------

    async def test_returns_task_url_when_sync_wait_elapses(self):
        submit = self._resp({"id": "task-pending"})
        running = self._resp({"status": "running"})
        # Many running polls; sync_wait is set to 0 so we exit immediately.
        client_instance = self._mock_async_client(submit, [running])

        with patch.dict(os.environ, {"SEEDANCE_SYNC_WAIT_S": "0"}):
            with patch("custom_handler_seedance.httpx.AsyncClient", return_value=client_instance):
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

        client_instance.post.assert_called_once()
        client_instance.get.assert_not_called()
        self.assertEqual(out.data[0].url, f"{TASK_URL_SCHEME}task-pending")
        self.assertEqual(out.data[0].revised_prompt, "submitted")

    async def test_async_submit_true_per_request(self):
        submit = self._resp({"id": "task-async-req"})
        client_instance = self._mock_async_client(submit, [])

        with patch("custom_handler_seedance.httpx.AsyncClient", return_value=client_instance):
            llm = SeedanceLLM()
            out = await llm.aimage_generation(
                model="dreamina-seedance-2-0-260128",
                prompt="p",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"async_submit": True},
                logging_obj=None,
            )

        client_instance.post.assert_called_once()
        client_instance.get.assert_not_called()
        self.assertEqual(out.data[0].url, f"{TASK_URL_SCHEME}task-async-req")
        self.assertEqual(out.data[0].revised_prompt, "submitted")

    # --- poll-only branch ----------------------------------------------------

    async def test_poll_task_id_returns_running_then_url(self):
        running = self._resp({"status": "running"})
        succeeded = self._resp(
            {
                "status": "succeeded",
                "usage": {"completion_tokens": 60_000},
                "content": {"video_url": "https://cdn.example/v-async.mp4"},
            }
        )

        # First call: wait_seconds=0 so we get the running placeholder back.
        running_only = self._mock_async_client(None, [running])
        with patch("custom_handler_seedance.httpx.AsyncClient", return_value=running_only):
            llm = SeedanceLLM()
            processing = await llm.aimage_generation(
                model="dreamina-seedance-2-0-260128",
                prompt="",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"seedance_task_id": "task-async", "wait_seconds": 0},
                logging_obj=None,
            )

        self.assertEqual(processing.data[0].url, f"{TASK_URL_SCHEME}task-async")
        self.assertEqual(processing.data[0].revised_prompt, "running")

        # Second call: default poll wait is 0 (single GET), not the submit sync window.
        success_only = self._mock_async_client(None, [succeeded])
        with patch("custom_handler_seedance.httpx.AsyncClient", return_value=success_only):
            completed = await SeedanceLLM().aimage_generation(
                model="dreamina-seedance-2-0-260128",
                prompt="",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"seedance_task_id": "task-async"},
                logging_obj=None,
            )

        self.assertEqual(completed.data[0].url, "https://cdn.example/v-async.mp4")
        # cost computed even on the poll-only path
        self.assertAlmostEqual(
            completed._hidden_params["response_cost"], 60_000 * 7.00 / 1_000_000
        )

    async def test_poll_ignores_async_submit_false(self):
        """async_submit:false on a poll must not expand wait to POLL_TIMEOUT_S."""
        running = self._resp({"status": "running"})
        client_instance = self._mock_async_client(None, [running])

        with patch("custom_handler_seedance.httpx.AsyncClient", return_value=client_instance):
            llm = SeedanceLLM()
            out = await llm.aimage_generation(
                model="dreamina-seedance-2-0-260128",
                prompt="",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"seedance_task_id": "task-no-block", "async_submit": False},
                logging_obj=None,
            )

        self.assertEqual(client_instance.get.await_count, 1)
        self.assertEqual(out.data[0].url, f"{TASK_URL_SCHEME}task-no-block")

    async def test_poll_without_wait_seconds_does_not_long_block(self):
        """Poll-only path must not inherit SEEDANCE_SYNC_WAIT_S (regression guard)."""
        running = self._resp({"status": "running"})
        client_instance = self._mock_async_client(None, [running])

        with patch("custom_handler_seedance.httpx.AsyncClient", return_value=client_instance):
            llm = SeedanceLLM()
            out = await llm.aimage_generation(
                model="dreamina-seedance-2-0-260128",
                prompt=f"{TASK_URL_SCHEME}task-slow",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"seedance_task_id": "task-slow"},
                logging_obj=None,
            )

        self.assertEqual(client_instance.get.await_count, 1)
        self.assertEqual(out.data[0].url, f"{TASK_URL_SCHEME}task-slow")
        self.assertEqual(out.data[0].revised_prompt, "running")

    async def test_submit_defaults_resolution_480p(self):
        submit = self._resp({"id": "task-res"})
        running = self._resp({"status": "running"})
        client_instance = self._mock_async_client(submit, [running])

        with patch.dict(os.environ, {"SEEDANCE_SYNC_WAIT_S": "0"}):
            with patch("custom_handler_seedance.httpx.AsyncClient", return_value=client_instance):
                await SeedanceLLM().aimage_generation(
                    model="dreamina-seedance-2-0-260128",
                    prompt="hello",
                    model_response=None,
                    api_key=None,
                    api_base=None,
                    optional_params={},
                    logging_obj=None,
                )

        body = client_instance.post.call_args.kwargs["json"]
        self.assertEqual(body["resolution"], "480p")
        self.assertEqual(body["ratio"], "1:1")
        self.assertEqual(body["duration"], 4)

    async def test_poll_task_url_in_prompt_is_accepted(self):
        succeeded = self._resp(
            {"status": "succeeded", "content": {"video_url": "https://cdn.example/done.mp4"}}
        )
        client_instance = self._mock_async_client(None, [succeeded])

        with patch("custom_handler_seedance.httpx.AsyncClient", return_value=client_instance):
            llm = SeedanceLLM()
            out = await llm.aimage_generation(
                model="dreamina-seedance-2-0-260128",
                prompt=f"{TASK_URL_SCHEME}task-from-prompt",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={},
                logging_obj=None,
            )

        self.assertEqual(out.data[0].url, "https://cdn.example/done.mp4")
        client_instance.get.assert_called_once()
        url = client_instance.get.call_args.args[0]
        self.assertTrue(url.endswith("/contents/generations/tasks/task-from-prompt"))

    # --- pricing helpers -----------------------------------------------------

    def test_fast_model_uses_fast_rate(self):
        llm = SeedanceLLM()
        cost = llm._compute_cost(
            ark_model="dreamina-seedance-2-0-fast-260128",
            usage={"completion_tokens": 1_000_000},
            has_input_video=False,
        )
        self.assertAlmostEqual(cost, 5.60)

        cost_with_video = llm._compute_cost(
            ark_model="dreamina-seedance-2-0-fast-260128",
            usage={"completion_tokens": 1_000_000},
            has_input_video=True,
        )
        self.assertAlmostEqual(cost_with_video, 3.30)

    def test_pro_model_default_rates(self):
        llm = SeedanceLLM()
        cost = llm._compute_cost(
            ark_model="dreamina-seedance-2-0-260128",
            usage={"completion_tokens": 1_000_000},
            has_input_video=False,
        )
        self.assertAlmostEqual(cost, 7.00)

        cost_with_video = llm._compute_cost(
            ark_model="dreamina-seedance-2-0-260128",
            usage={"completion_tokens": 1_000_000},
            has_input_video=True,
        )
        self.assertAlmostEqual(cost_with_video, 4.30)

    async def test_repeated_poll_of_completed_task_does_not_double_bill(self):
        succeeded = self._resp(
            {
                "status": "succeeded",
                "usage": {"completion_tokens": 100_000},
                "content": {"video_url": "https://x/done.mp4"},
            }
        )

        # First poll -> cost attributed.
        first = self._mock_async_client(None, [succeeded])
        llm = SeedanceLLM()
        with patch("custom_handler_seedance.httpx.AsyncClient", return_value=first):
            out1 = await llm.aimage_generation(
                model="dreamina-seedance-2-0-260128",
                prompt="",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"seedance_task_id": "task-dedup", "wait_seconds": 0},
                logging_obj=None,
            )
        self.assertAlmostEqual(out1._hidden_params["response_cost"], 100_000 * 7.00 / 1_000_000)

        # Re-poll same task -> cost not re-attributed.
        succeeded2 = self._resp(
            {
                "status": "succeeded",
                "usage": {"completion_tokens": 100_000},
                "content": {"video_url": "https://x/done.mp4"},
            }
        )
        second = self._mock_async_client(None, [succeeded2])
        with patch("custom_handler_seedance.httpx.AsyncClient", return_value=second):
            out2 = await llm.aimage_generation(
                model="dreamina-seedance-2-0-260128",
                prompt="",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"seedance_task_id": "task-dedup", "wait_seconds": 0},
                logging_obj=None,
            )
        self.assertNotIn("response_cost", out2._hidden_params)
        self.assertEqual(out2.data[0].url, "https://x/done.mp4")

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

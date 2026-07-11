import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks, HTTPException
from starlette.requests import Request

from callback_server import byteplus_callback
from generation_job_adapters import (
    BytePlusAdapter,
    ProviderAdapterError,
    XAIAdapter,
    _validate_public_https_url,
    provider_for_model,
)
from generation_job_models import GenerationJobCreate, safe_client_metadata
from generation_job_scheduler import next_poll_time


def callback_request(body: bytes, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {"type": "http", "method": "POST", "path": "/callback", "headers": headers or []},
        receive,
    )


class GenerationJobModelTests(unittest.TestCase):
    def test_generation_routes_are_available_to_llm_api_virtual_keys(self):
        from litellm.proxy._types import UserAPIKeyAuth
        from litellm.proxy.auth.route_checks import RouteChecks

        from gateway_server import register_generation_job_llm_routes

        register_generation_job_llm_routes()
        virtual_key = UserAPIKeyAuth(api_key="hashed-test-key", allowed_routes=["llm_api_routes"])
        for route in (
            "/v1/generation-jobs",
            "/v1/generation-jobs/gen_test_123",
            "/v1/generation-jobs/gen_test_123/content",
        ):
            self.assertTrue(RouteChecks.is_virtual_key_allowed_to_call_route(route, virtual_key))
            self.assertTrue(RouteChecks.is_llm_api_route(route))

    def test_provider_mapping(self):
        self.assertEqual(provider_for_model("grok-video-1.5"), "xai")
        self.assertEqual(provider_for_model("seedance-2.0"), "byteplus")
        self.assertEqual(provider_for_model("veo-3.1-fast"), "vertex")

    def test_media_rejects_non_https_url(self):
        for url in ("http://unsafe.example/image.png", "data:image/png;base64,AAAA"):
            with self.assertRaises(ValueError):
                GenerationJobCreate.model_validate(
                    {
                        "model": "seedance-2.0",
                        "prompt": "test",
                        "media_inputs": [{"type": "image", "url": url}],
                    }
                )

    def test_poll_cadence_caps_at_twenty_seconds_with_jitter(self):
        now = datetime.now(timezone.utc)
        for attempt, minimum, maximum in ((0, 4, 6), (1, 8, 12), (2, 16, 24), (20, 16, 24)):
            delay = (next_poll_time(attempt, now=now) - now).total_seconds()
            self.assertGreaterEqual(delay, minimum)
            self.assertLessEqual(delay, maximum)

    def test_persisted_client_metadata_excludes_arbitrary_prompt_like_values(self):
        self.assertEqual(
            safe_client_metadata(
                {"source": "experience_runner", "run_id": "run_1", "prompt": "secret prompt", "note": "private"}
            ),
            {"source": "experience_runner", "run_id": "run_1"},
        )


class ProviderAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_download_rejects_private_addresses(self):
        with self.assertRaises(ProviderAdapterError):
            await _validate_public_https_url("https://127.0.0.1/internal")

    async def test_xai_submit_returns_request_id_without_polling(self):
        request = GenerationJobCreate(
            model="grok-video",
            prompt="A lighthouse in a storm",
            duration_seconds=8,
            resolution="720p",
        )
        with patch.dict(os.environ, {"GROK_API_KEY": "test-key"}), patch(
            "generation_job_adapters._json_request", new=AsyncMock(return_value={"request_id": "req_123", "status": "pending"})
        ) as call:
            result = await XAIAdapter().submit(request, job_id="gen_1", callback_url=None)
        self.assertEqual(result.provider_request_id, "req_123")
        self.assertEqual(call.await_count, 1)
        self.assertEqual(call.await_args.args[0], "POST")

    async def test_xai_15_uploads_starting_frame_to_files_before_submission(self):
        request = GenerationJobCreate(
            model="grok-video-1.5",
            prompt="Animate this frame",
            media_inputs=[
                {"type": "image", "role": "first_frame", "url": "https://storage.example/frame.png"}
            ],
        )
        adapter = XAIAdapter()
        with patch.dict(os.environ, {"GROK_API_KEY": "test-key"}), patch.object(
            adapter, "_upload_file", new=AsyncMock(return_value={"file_id": "file_123"})
        ) as upload, patch(
            "generation_job_adapters._json_request",
            new=AsyncMock(return_value={"request_id": "req_15", "status": "pending"}),
        ) as submit:
            result = await adapter.submit(request, job_id="gen_15", callback_url=None)
        self.assertEqual(result.provider_request_id, "req_15")
        self.assertEqual(upload.await_count, 1)
        self.assertEqual(submit.await_args.kwargs["body"]["image"], {"file_id": "file_123"})

    async def test_xai_terminal_status_uses_provider_cost_ticks(self):
        job = {
            "provider_request_id": "req_123",
            "request_metadata": {"duration_seconds": 8, "resolution": "720p"},
        }
        data = {
            "status": "done",
            "video": {"url": "https://example.com/video.mp4"},
            "usage": {"cost_in_usd_ticks": 700_000_000},
        }
        with patch.dict(os.environ, {"GROK_API_KEY": "test-key"}), patch(
            "generation_job_adapters._json_request", new=AsyncMock(return_value=data)
        ):
            result = await XAIAdapter().retrieve(job)
        self.assertEqual(result.status, "completed")
        self.assertAlmostEqual(result.cost_usd, 0.07)

    async def test_byteplus_submission_includes_callback_and_persists_video_rate_input(self):
        request = GenerationJobCreate(
            model="seedance-2.0",
            prompt="Restyle the shot",
            media_inputs=[{"type": "video", "role": "source", "url": "https://example.com/input.mp4"}],
        )
        mocked = AsyncMock(return_value={"id": "task_123", "status": "queued"})
        with patch.dict(os.environ, {"BYTEDANCE_API_KEY": "test-key"}), patch(
            "generation_job_adapters._json_request", new=mocked
        ):
            result = await BytePlusAdapter().submit(
                request,
                job_id="gen_1",
                callback_url="https://callbacks.example/callback?token=secret",
            )
        self.assertEqual(result.provider_request_id, "task_123")
        self.assertTrue(result.request_metadata["has_input_video"])
        self.assertEqual(mocked.await_args.kwargs["body"]["callback_url"], "https://callbacks.example/callback?token=secret")

    async def test_byteplus_terminal_cost_uses_persisted_video_rate(self):
        job = {
            "provider_request_id": "task_123",
            "request_metadata": {"has_input_video": True, "upstream_model": "dreamina-seedance-2-0-260128"},
        }
        data = {
            "status": "succeeded",
            "model": "dreamina-seedance-2-0-260128",
            "content": {"video_url": "https://example.com/output.mp4"},
            "usage": {"completion_tokens": 1_000_000},
        }
        with patch.dict(os.environ, {"BYTEDANCE_API_KEY": "test-key"}), patch(
            "generation_job_adapters._json_request", new=AsyncMock(return_value=data)
        ):
            result = await BytePlusAdapter().retrieve(job)
        self.assertEqual(result.status, "completed")
        self.assertAlmostEqual(result.cost_usd, 4.30)

    async def test_byteplus_accepts_multipart_image_without_persisting_media(self):
        request = GenerationJobCreate(
            model="seedance-2.0",
            prompt="Animate the frame",
            media_inputs=[{"type": "image", "role": "first_frame", "upload_field": "frame"}],
        )
        mocked = AsyncMock(return_value={"id": "task_upload", "status": "queued"})
        with patch.dict(os.environ, {"BYTEDANCE_API_KEY": "test-key"}), patch(
            "generation_job_adapters._json_request", new=mocked
        ):
            await BytePlusAdapter().submit(
                request,
                job_id="gen_upload",
                callback_url=None,
                upload_bytes={"frame": ("frame.png", b"png-bytes", "image/png")},
            )
        content = mocked.await_args.kwargs["body"]["content"]
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_records_then_enqueues_verification_after_acknowledgement(self):
        background = BackgroundTasks()
        record = AsyncMock(return_value={"id": "gen_callback"})
        enqueue = AsyncMock(return_value=True)
        with patch.object(__import__("callback_server").repository, "record_callback", new=record), patch(
            "callback_server.enqueue_poll", new=enqueue
        ):
            result = await byteplus_callback(
                "gen_callback", callback_request(b'{"status":"succeeded"}'), background, token="callback-token"
            )
            self.assertEqual(result, {"accepted": True})
            self.assertEqual(enqueue.await_count, 0)
            await background()
        self.assertEqual(enqueue.await_count, 1)
        self.assertEqual(record.await_count, 1)

    async def test_callback_rejects_invalid_token_and_malformed_or_oversized_body(self):
        background = BackgroundTasks()
        with patch.object(
            __import__("callback_server").repository, "record_callback", new=AsyncMock(return_value=None)
        ):
            with self.assertRaises(HTTPException) as invalid:
                await byteplus_callback("gen_missing", callback_request(b"{}"), background, token="wrong")
        self.assertEqual(invalid.exception.status_code, 404)

        with self.assertRaises(HTTPException) as malformed:
            await byteplus_callback("gen_bad", callback_request(b"not-json"), background, token="token")
        self.assertEqual(malformed.exception.status_code, 400)

        with patch.dict(os.environ, {"GENERATION_CALLBACK_MAX_BYTES": "4"}):
            with self.assertRaises(HTTPException) as oversized:
                await byteplus_callback("gen_large", callback_request(b"12345"), background, token="token")
        self.assertEqual(oversized.exception.status_code, 413)


if __name__ == "__main__":
    unittest.main()

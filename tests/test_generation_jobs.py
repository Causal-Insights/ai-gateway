import os
import base64
import struct
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks, HTTPException
from starlette.requests import Request

from callback_server import byteplus_callback
from generation_job_adapters import (
    BytePlusAdapter,
    ProviderAdapterError,
    VertexAdapter,
    XAIAdapter,
    _validate_public_https_url,
    provider_for_model,
)
from generation_job_models import GenerationJobCreate, safe_client_metadata
from generation_job_scheduler import enqueue_poll, next_poll_time


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
        self.assertEqual(provider_for_model("gemini-omni-flash"), "vertex")
        self.assertEqual(provider_for_model("gemini-omni-flash-preview"), "vertex")

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


class GenerationJobSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_polling_takes_precedence_over_cloud_tasks(self):
        when = datetime.now(timezone.utc) + timedelta(seconds=30)
        with patch.dict(os.environ, {"GENERATION_LOCAL_POLLING": "true"}), patch(
            "generation_job_scheduler._schedule_local_poll"
        ) as schedule_local, patch(
            "generation_job_scheduler._queue_config"
        ) as queue_config:
            queued = await enqueue_poll("gen_local", when)
        self.assertTrue(queued)
        schedule_local.assert_called_once_with("gen_local", when)
        queue_config.assert_not_called()


class ProviderAdapterTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _mp4_with_duration(seconds: float) -> bytes:
        mvhd_payload = b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, 1000, int(seconds * 1000))
        mvhd = struct.pack(">I4s", 8 + len(mvhd_payload), b"mvhd") + mvhd_payload
        return struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd

    def test_mp4_duration_parser(self):
        self.assertAlmostEqual(VertexAdapter._mp4_duration_seconds(self._mp4_with_duration(9.5)), 9.5)

    def test_omni_cost_uses_video_token_rate(self):
        usage = {
            "total_input_tokens": 10,
            "total_output_tokens": 100,
            "total_thought_tokens": 2,
            "output_tokens_by_modality": [{"modality": "video", "tokens": 80}],
        }
        with patch.dict("sys.modules", {"litellm": None}):
            cost = VertexAdapter._omni_cost(usage)
        self.assertAlmostEqual(cost, 0.001613)

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
        with patch.dict(
            os.environ,
            {"GROK_API_KEY": "test-key"},
        ), patch.object(
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

    async def test_xai_15_references_and_voices_use_exact_model(self):
        request = GenerationJobCreate(
            model="grok-video-1.5",
            prompt="Characters speak",
            duration_seconds=5,
            resolution="720p",
            reference_voice_ids=["eve", "leo"],
            media_inputs=[
                {"type": "image", "role": "reference", "url": "https://example.com/one.png"},
                {"type": "image", "role": "reference", "url": "https://example.com/two.png"},
            ],
        )
        mocked = AsyncMock(return_value={"request_id": "req_ref", "status": "pending"})
        with patch.dict(
            os.environ,
            {"GROK_API_KEY": "test-key"},
        ), patch(
            "generation_job_adapters._json_request", new=mocked
        ):
            await XAIAdapter().submit(request, job_id="gen_ref", callback_url=None)
        payload = mocked.await_args.kwargs["body"]
        self.assertEqual(payload["model"], "grok-imagine-video-1.5")
        self.assertEqual(payload["reference_audios"], [{"voice_id": "eve"}, {"voice_id": "leo"}])
        self.assertEqual(len(payload["reference_images"]), 2)

    async def test_xai_15_rejects_unverified_video_edit(self):
        request = GenerationJobCreate(
            model="grok-video-1.5",
            prompt="Restyle",
            operation="edit",
            media_inputs=[{"type": "video", "role": "source", "url": "https://example.com/in.mp4"}],
        )
        with patch.dict(
            os.environ,
            {
                "GROK_API_KEY": "test-key",
                "GROK_VIDEO_15_VIDEO_OPERATIONS_VERIFIED": "false",
            },
        ), self.assertRaises(
            ProviderAdapterError
        ) as caught:
            await XAIAdapter().submit(request, job_id="gen_edit", callback_url=None)
        self.assertEqual(caught.exception.code, "CAPABILITY_NOT_VERIFIED")

    async def test_xai_15_generation_is_enabled_by_default(self):
        request = GenerationJobCreate(model="grok-video-1.5", prompt="A sunrise")
        mocked = AsyncMock(return_value={"request_id": "req_enabled", "status": "pending"})
        with patch.dict(
            os.environ,
            {"GROK_API_KEY": "test-key"},
        ), patch("generation_job_adapters._json_request", new=mocked):
            result = await XAIAdapter().submit(request, job_id="gen_enabled", callback_url=None)
        self.assertEqual(result.provider_request_id, "req_enabled")
        self.assertEqual(mocked.await_args.kwargs["body"]["model"], "grok-imagine-video-1.5")

    async def test_xai_15_verified_video_edit_keeps_exact_model(self):
        request = GenerationJobCreate(
            model="grok-video-1.5",
            prompt="Restyle",
            operation="edit",
            media_inputs=[{"type": "video", "role": "source", "url": "https://example.com/in.mp4"}],
        )
        mocked = AsyncMock(return_value={"request_id": "req_edit_15", "status": "pending"})
        with patch.dict(
            os.environ,
            {"GROK_API_KEY": "test-key", "GROK_VIDEO_15_VIDEO_OPERATIONS_VERIFIED": "true"},
        ), patch("generation_job_adapters._json_request", new=mocked):
            await XAIAdapter().submit(request, job_id="gen_edit_15", callback_url=None)
        self.assertTrue(mocked.await_args.args[1].endswith("/videos/edits"))
        self.assertEqual(mocked.await_args.kwargs["body"]["model"], "grok-imagine-video-1.5")

    async def test_legacy_xai_extension_uses_extension_endpoint(self):
        request = GenerationJobCreate(
            model="grok-video",
            prompt="Continue",
            operation="extend",
            duration_seconds=4,
            media_inputs=[{"type": "video", "role": "source", "url": "https://example.com/in.mp4"}],
        )
        mocked = AsyncMock(return_value={"request_id": "req_extend", "status": "pending"})
        with patch.dict(os.environ, {"GROK_API_KEY": "test-key"}), patch(
            "generation_job_adapters._json_request", new=mocked
        ):
            await XAIAdapter().submit(request, job_id="gen_extend", callback_url=None)
        self.assertTrue(mocked.await_args.args[1].endswith("/videos/extensions"))
        self.assertEqual(mocked.await_args.kwargs["body"]["duration"], 4)

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

    async def test_omni_submission_maps_first_frame_and_references(self):
        request = GenerationJobCreate(
            model="gemini-omni-flash-preview",
            prompt="The person walks through the scene",
            duration_seconds=6,
            aspect_ratio="9:16",
            media_inputs=[
                {"type": "image", "role": "first_frame", "upload_field": "first"},
                {"type": "image", "role": "reference", "upload_field": "reference"},
            ],
        )
        mocked = AsyncMock(return_value={"id": "v1_interaction", "status": "in_progress"})
        adapter = VertexAdapter()
        with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "project-1"}), patch.object(
            adapter, "_vertex_headers", new=AsyncMock(return_value={"Authorization": "Bearer token"})
        ), patch("generation_job_adapters._json_request", new=mocked):
            result = await adapter.submit(
                request,
                job_id="gen_omni",
                callback_url=None,
                upload_bytes={
                    "first": ("first.png", b"first", "image/png"),
                    "reference": ("reference.jpg", b"reference", "image/jpeg"),
                },
            )
        body = mocked.await_args.kwargs["body"]
        self.assertEqual(result.provider_request_id, "v1_interaction")
        self.assertEqual(body["model"], "gemini-omni-flash-preview")
        self.assertTrue(body["background"])
        self.assertTrue(body["store"])
        self.assertEqual(body["generation_config"]["video_config"]["task"], "reference_to_video")
        prompt = body["input"][0]["content"][-1]["text"]
        self.assertIn("<FIRST_FRAME>@Image1", prompt)
        self.assertIn("<IMAGE_REF_0>@Image2", prompt)

    async def test_omni_stateful_edit_uses_resolved_interaction_id(self):
        request = GenerationJobCreate(
            model="gemini-omni-flash",
            prompt="Make the lighting dramatic",
            operation="edit",
            previous_job_id="gen_previous",
        )
        request._previous_interaction_id = "v1_previous"
        mocked = AsyncMock(return_value={"id": "v1_next", "status": "in_progress"})
        adapter = VertexAdapter()
        with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "project-1"}), patch.object(
            adapter, "_vertex_headers", new=AsyncMock(return_value={"Authorization": "Bearer token"})
        ), patch("generation_job_adapters._json_request", new=mocked):
            await adapter.submit(request, job_id="gen_next", callback_url=None)
        self.assertEqual(mocked.await_args.kwargs["body"]["previous_interaction_id"], "v1_previous")

    async def test_omni_source_video_edit_inherits_source_aspect_ratio(self):
        request = GenerationJobCreate(
            model="gemini-omni-flash-preview",
            prompt="Replace the background",
            operation="edit",
            media_inputs=[{"type": "video", "role": "source", "upload_field": "source"}],
        )
        mocked = AsyncMock(return_value={"id": "v1_edit", "status": "in_progress"})
        adapter = VertexAdapter()
        with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "project-1"}), patch.object(
            adapter, "_vertex_headers", new=AsyncMock(return_value={"Authorization": "Bearer token"})
        ), patch("generation_job_adapters._json_request", new=mocked):
            await adapter.submit(
                request,
                job_id="gen_edit",
                callback_url=None,
                upload_bytes={
                    "source": ("source.mp4", self._mp4_with_duration(3), "video/mp4")
                },
            )
        body = mocked.await_args.kwargs["body"]
        self.assertEqual(body["generation_config"]["video_config"]["task"], "edit")
        self.assertEqual(body["response_format"], {"type": "video"})

    async def test_omni_source_video_edit_rejects_aspect_ratio_override(self):
        request = GenerationJobCreate(
            model="gemini-omni-flash-preview",
            prompt="Replace the background",
            operation="edit",
            aspect_ratio="16:9",
            media_inputs=[{"type": "video", "role": "source", "upload_field": "source"}],
        )
        with self.assertRaises(ProviderAdapterError) as caught:
            await VertexAdapter().submit(
                request,
                job_id="gen_edit_ratio",
                callback_url=None,
                upload_bytes={
                    "source": ("source.mp4", self._mp4_with_duration(3), "video/mp4")
                },
            )
        self.assertEqual(caught.exception.code, "INVALID_REQUEST")

    async def test_omni_retrieve_and_content_parse_inline_video(self):
        encoded = base64.b64encode(b"mp4-bytes").decode()
        data = {
            "id": "v1_done",
            "status": "completed",
            "steps": [
                {
                    "type": "model_output",
                    "content": [{"type": "video", "mime_type": "video/mp4", "data": encoded}],
                }
            ],
            "usage": {"total_input_tokens": 10, "total_output_tokens": 20},
        }
        job = {
            "model": "gemini-omni-flash-preview",
            "provider_request_id": "v1_done",
            "request_metadata": {"upstream_model": "gemini-omni-flash-preview"},
        }
        adapter = VertexAdapter()
        with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "project-1"}), patch.object(
            adapter, "_vertex_headers", new=AsyncMock(return_value={"Authorization": "Bearer token"})
        ), patch.object(adapter, "_omni_cost", return_value=0.25), patch(
            "generation_job_adapters._json_request", new=AsyncMock(return_value=data)
        ):
            status = await adapter.retrieve(job)
            content = await adapter.content(job)
        self.assertEqual(status.status, "completed")
        self.assertEqual(status.cost_usd, 0.25)
        self.assertEqual(content.content, b"mp4-bytes")

    async def test_omni_rejects_extension_and_last_frame(self):
        request = GenerationJobCreate(
            model="gemini-omni-flash-preview",
            prompt="Interpolate",
            operation="extend",
            media_inputs=[{"type": "image", "role": "last_frame", "upload_field": "last"}],
        )
        with self.assertRaises(ProviderAdapterError) as caught:
            await VertexAdapter().submit(
                request,
                job_id="gen_invalid",
                callback_url=None,
                upload_bytes={"last": ("last.png", b"last", "image/png")},
            )
        self.assertEqual(caught.exception.code, "CAPABILITY_NOT_SUPPORTED")

    async def test_omni_rejects_voice_editing(self):
        request = GenerationJobCreate(
            model="gemini-omni-flash-preview",
            prompt="Use a supplied voice",
            reference_voice_ids=["eve"],
        )
        with self.assertRaises(ProviderAdapterError) as caught:
            await VertexAdapter().submit(request, job_id="gen_voice", callback_url=None)
        self.assertEqual(caught.exception.code, "CAPABILITY_NOT_SUPPORTED")

    async def test_omni_rejects_source_video_over_ten_seconds(self):
        request = GenerationJobCreate(
            model="gemini-omni-flash-preview",
            prompt="Restyle the source",
            operation="edit",
            media_inputs=[{"type": "video", "role": "source", "upload_field": "source"}],
        )
        with self.assertRaises(ProviderAdapterError) as caught:
            await VertexAdapter().submit(
                request,
                job_id="gen_long",
                callback_url=None,
                upload_bytes={"source": ("source.mp4", self._mp4_with_duration(10.1), "video/mp4")},
            )
        self.assertEqual(caught.exception.code, "INVALID_MEDIA_INPUT")


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

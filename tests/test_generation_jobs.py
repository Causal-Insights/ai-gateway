import json
import os
import base64
import struct
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks, HTTPException
from starlette.requests import Request

from callback_server import byteplus_callback
from generation_job_adapters import (
    BytePlusAdapter,
    ProviderAdapterError,
    VertexAdapter,
    VertexVeoDirectAdapter,
    XAIAdapter,
    _json_request,
    _validate_public_https_url,
    provider_for_model,
    route_for,
)
from generation_job_models import GenerationJobCreate, GenerationJobCreateV2, safe_client_metadata
from generation_job_routes import _hash_request, _hash_request_v2, _is_compatible_omni_previous_job, _parse_request
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
        self.assertEqual(provider_for_model("seedance-2.5"), "byteplus")
        self.assertEqual(provider_for_model("veo-3.1-fast"), "vertex")
        self.assertEqual(route_for("grok-video-1.5", 1), "xai_videos_v1")
        self.assertEqual(route_for("seedance-2.5", 1), "byteplus_las_v1")
        self.assertEqual(route_for("seedance-2.0", 1), "byteplus_ark_v3")
        self.assertEqual(route_for("veo-3.1-fast", 1), "vertex_litellm_video")
        self.assertEqual(route_for("gemini-omni-flash", 1), "vertex_omni_interactions")
        self.assertEqual(route_for("grok-video-1.5", 2), "xai_videos_v2")
        self.assertEqual(route_for("seedance-2.0", 2), "byteplus_ark_v3")
        self.assertEqual(route_for("veo-3.1-fast", 2), "vertex_litellm_video")
        with self.assertRaises(ProviderAdapterError) as raised:
            route_for("gemini-omni-flash", 2)
        self.assertEqual(raised.exception.code, "UNSUPPORTED_MODEL")
        with self.assertRaises(ProviderAdapterError) as seedance_25:
            route_for("seedance-2.5", 2)
        self.assertEqual(seedance_25.exception.code, "UNSUPPORTED_MODEL")
        for model in (
            "gemini-omni-flash",
            "gemini-omni-flash-preview",
            "gemini-omni-1.1-flash",
            "gemini-omni-1.1-flash-preview",
            "vertex_ai/gemini-omni-flash-preview",
            "vertex_ai/gemini-omni-1.1-flash-preview",
        ):
            with self.subTest(model=model):
                self.assertEqual(provider_for_model(model), "vertex")

    def test_original_omni_interactions_can_continue_through_every_1_1_alias(self):
        previous = {
            "status": "completed",
            "provider": "vertex",
            "model": "gemini-omni-flash-preview",
            "provider_request_id": "interaction_original",
        }
        for requested_model in (
            "gemini-omni-flash",
            "gemini-omni-flash-preview",
            "gemini-omni-1.1-flash",
            "gemini-omni-1.1-flash-preview",
        ):
            with self.subTest(requested_model=requested_model):
                self.assertTrue(_is_compatible_omni_previous_job(requested_model, previous))
        self.assertFalse(_is_compatible_omni_previous_job("veo-3.1-fast", previous))
        self.assertFalse(_is_compatible_omni_previous_job("gemini-omni-1.1-flash", {**previous, "provider_request_id": None}))

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


FIXTURES_V1 = Path(__file__).resolve().parent / "fixtures" / "generation_jobs_v1"
FIXTURES_V2 = Path(__file__).resolve().parent / "fixtures" / "generation_jobs_v2"
GROK_V2_GOLDEN = "gj2:021158aa5b5e9bc8bcb77ab092977ed7ce7d4a76580097eaa04f05786581c370"


class GenerationJobHashTests(unittest.TestCase):
    def test_v1_fixtures_reproduce_frozen_hashes(self):
        files = sorted(FIXTURES_V1.glob("*.json"))
        self.assertGreaterEqual(len(files), 4)
        for path in files:
            record = json.loads(path.read_text())
            payload = GenerationJobCreate.model_validate(record["body"])
            self.assertEqual(_hash_request(payload, {}), record["request_hash"], path.name)

    def test_v2_grok_text_hash_is_pinned(self):
        record = json.loads((FIXTURES_V2 / "grok_video_15_text.json").read_text())
        payload = GenerationJobCreateV2.model_validate(record["body"])
        digest = _hash_request_v2(payload, {})
        self.assertEqual(digest, GROK_V2_GOLDEN)
        self.assertEqual(digest, record["request_hash"])
        self.assertTrue(digest.startswith("gj2:"))


class GenerationJobV2ParseTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _json_request(body: dict) -> Request:
        encoded = json.dumps(body).encode()

        async def receive():
            return {"type": "http.request", "body": encoded, "more_body": False}

        return Request(
            {
                "type": "http",
                "asgi": {"spec_version": "2.3", "version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/v1/generation-jobs",
                "raw_path": b"/v1/generation-jobs",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json"), (b"host", b"test")],
                "client": ("127.0.0.1", 123),
                "server": ("test", 80),
            },
            receive,
        )

    async def test_v2_grok_validates_and_hashes(self):
        body = json.loads((FIXTURES_V2 / "grok_video_15_text.json").read_text())["body"]
        payload, uploads = await _parse_request(self._json_request(body))
        self.assertIsInstance(payload, GenerationJobCreateV2)
        self.assertEqual(_hash_request_v2(payload, uploads), GROK_V2_GOLDEN)
        self.assertEqual(route_for(payload.model, 2), "xai_videos_v2")

    async def test_schema_3_is_unsupported(self):
        with self.assertRaises(HTTPException) as raised:
            await _parse_request(self._json_request({"request_schema_version": 3, "model": "grok-video-1.5"}))
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail["code"], "UNSUPPORTED_REQUEST_SCHEMA")

    async def test_v2_omni_has_no_route(self):
        body = {
            "request_schema_version": 2,
            "model": "gemini-omni-flash",
            "contract_revision": "1",
            "profile_id": "generate.text",
            "operation": "generate",
            "prompt": "hello",
        }
        payload, _uploads = await _parse_request(self._json_request(body))
        with self.assertRaises(ProviderAdapterError) as raised:
            route_for(payload.model, 2)
        self.assertEqual(raised.exception.code, "UNSUPPORTED_MODEL")


class VertexVeoDirectAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_sends_predict_long_running_camel_case(self):
        request = GenerationJobCreateV2(
            request_schema_version=2,
            model="veo-3.1-fast",
            contract_revision="1",
            profile_id="generate.text",
            operation="generate",
            prompt="sunlit conservatory",
            settings={"resolution": "1080p", "duration": 8, "aspectRatio": "16:9", "generateAudio": True},
        )
        mocked = AsyncMock(return_value={"name": "projects/p/locations/us-central1/operations/op1"})
        with patch.dict(
            os.environ,
            {
                "GOOGLE_CLOUD_PROJECT": "p",
                "VERTEX_LOCATION": "us-central1",
                "VEO_OUTPUT_GCS_PREFIX": "gs://ml-veo-scratch/jobs",
            },
        ), patch(
            "generation_job_adapters.VertexAdapter._vertex_headers",
            new=AsyncMock(return_value={"Authorization": "Bearer t", "Content-Type": "application/json"}),
        ), patch("generation_job_adapters._json_request", new=mocked):
            result = await VertexVeoDirectAdapter().submit(request, job_id="gen_veo", callback_url=None)
        self.assertEqual(result.provider_request_id, "projects/p/locations/us-central1/operations/op1")
        url = mocked.await_args.args[1]
        self.assertIn(":predictLongRunning", url)
        body = mocked.await_args.kwargs["body"]
        self.assertEqual(body["parameters"]["aspectRatio"], "16:9")
        self.assertEqual(body["parameters"]["resolution"], "1080p")
        self.assertEqual(body["parameters"]["generateAudio"], True)
        self.assertEqual(body["parameters"]["storageUri"], "gs://ml-veo-scratch/jobs/gen_veo/")


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

    async def test_submission_rate_limit_is_a_definitive_provider_rejection(self):
        import httpx

        request = httpx.Request("POST", "https://provider.example/interactions")
        response = httpx.Response(
            429,
            request=request,
            json={"error": {"message": "Quota exceeded"}},
        )
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.request.return_value = response
        with patch("generation_job_adapters.httpx.AsyncClient", return_value=client):
            with self.assertRaises(ProviderAdapterError) as caught:
                await _json_request(
                    "POST",
                    str(request.url),
                    headers={"Authorization": "Bearer token"},
                    body={"model": "gemini-omni-1.1-flash-preview"},
                    submission=True,
                )
        self.assertEqual(caught.exception.code, "PROVIDER_RATE_LIMITED")
        self.assertTrue(caught.exception.retryable)
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertEqual(caught.exception.status_code, 429)

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

    async def test_byteplus_maps_reference_role_and_allows_nine_images(self):
        request = GenerationJobCreate(
            model="seedance-2.0",
            prompt="Keep the referenced subjects consistent",
            media_inputs=[
                {
                    "type": "image",
                    "role": "reference",
                    "url": f"https://example.com/reference-{index}.png",
                }
                for index in range(9)
            ],
        )
        mocked = AsyncMock(return_value={"id": "task_refs", "status": "queued"})
        with patch.dict(os.environ, {"BYTEDANCE_API_KEY": "test-key"}), patch(
            "generation_job_adapters._json_request", new=mocked
        ):
            await BytePlusAdapter().submit(request, job_id="gen_refs", callback_url=None)
        content = mocked.await_args.kwargs["body"]["content"]
        self.assertEqual(len(content), 10)
        self.assertTrue(all(item["role"] == "reference_image" for item in content[1:]))

    async def test_byteplus_rejects_tenth_seedance_2_image(self):
        request = GenerationJobCreate(
            model="seedance-2.0",
            prompt="Too many references",
            media_inputs=[
                {
                    "type": "image",
                    "role": "reference",
                    "url": f"https://example.com/reference-{index}.png",
                }
                for index in range(10)
            ],
        )
        with patch.dict(os.environ, {"BYTEDANCE_API_KEY": "test-key"}), self.assertRaises(
            ProviderAdapterError
        ) as caught:
            await BytePlusAdapter().submit(request, job_id="gen_refs", callback_url=None)
        self.assertEqual(caught.exception.code, "INVALID_MEDIA_INPUT")

    async def test_seedance_2_5_uses_las_endpoint_and_exact_upstream_model(self):
        request = GenerationJobCreate(
            model="seedance-2.5",
            prompt="A cinematic garden in the rain",
            duration_seconds=30,
            resolution="480p",
            aspect_ratio="16:9",
            generate_audio=True,
            media_inputs=[
                {
                    "type": "image",
                    "role": "reference",
                    "url": f"https://example.com/reference-{index}.png",
                }
                for index in range(30)
            ],
        )
        mocked = AsyncMock(return_value={"id": "task_25", "status": "queued"})
        with patch.dict(
            os.environ,
            {
                "SEEDANCE_2_5_API_KEY": "test-key",
                "SEEDANCE_2_5_BASE_URL": "https://las.example/api/v1",
            },
        ), patch("generation_job_adapters._json_request", new=mocked):
            result = await BytePlusAdapter().submit(
                request, job_id="gen_25", callback_url=None
            )
        body = mocked.await_args.kwargs["body"]
        self.assertEqual(
            mocked.await_args.args[1],
            "https://las.example/api/v1/contents/generations/tasks",
        )
        self.assertEqual(body["model"], "dreamina-seedance-2-5-260628")
        self.assertEqual(body["resolution"], "480p")
        self.assertEqual(body["duration"], 30)
        self.assertEqual(len(body["content"]), 31)
        self.assertEqual(result.request_metadata["upstream_model"], "dreamina-seedance-2-5-260628")

    async def test_seedance_2_5_rejects_unpublished_modes_and_settings(self):
        cases = (
            GenerationJobCreate(
                model="seedance-2.5",
                prompt="edit",
                operation="edit",
                media_inputs=[
                    {"type": "video", "role": "source", "url": "https://example.com/source.mp4"}
                ],
            ),
            GenerationJobCreate(
                model="seedance-2.5", prompt="too long", duration_seconds=31
            ),
            GenerationJobCreate(
                model="seedance-2.5", prompt="too large", resolution="1080p"
            ),
        )
        for request in cases:
            with self.subTest(request=request), patch.dict(
                os.environ, {"SEEDANCE_2_5_API_KEY": "test-key"}
            ), self.assertRaises(ProviderAdapterError):
                await BytePlusAdapter().submit(request, job_id="gen_invalid", callback_url=None)

    async def test_seedance_2_5_terminal_cost_uses_resolution_and_duration(self):
        job = {
            "model": "seedance-2.5",
            "provider_request_id": "task_25",
            "request_metadata": {
                "upstream_model": "dreamina-seedance-2-5-260628",
                "duration_seconds": 4,
                "resolution": "720p",
                "has_input_video": False,
            },
        }
        data = {
            "status": "succeeded",
            "model": "dreamina-seedance-2-5-260628",
            "content": {"video_url": "https://example.com/output.mp4"},
            "usage": {},
        }
        with patch.dict(os.environ, {"SEEDANCE_2_5_API_KEY": "test-key"}), patch(
            "generation_job_adapters._json_request", new=AsyncMock(return_value=data)
        ):
            result = await BytePlusAdapter().retrieve(job)
        self.assertEqual(result.status, "completed")
        self.assertAlmostEqual(result.cost_usd, 4 * 0.462075)

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
        self.assertEqual(body["model"], "gemini-omni-1.1-flash-preview")
        self.assertTrue(body["background"])
        self.assertTrue(body["store"])
        self.assertEqual(body["generation_config"]["video_config"]["task"], "reference_to_video")
        self.assertEqual(
            body["response_format"],
            {"type": "video", "resolution": "720p", "duration": "6s", "aspect_ratio": "9:16"},
        )
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
        body = mocked.await_args.kwargs["body"]
        self.assertEqual(body["previous_interaction_id"], "v1_previous")
        self.assertNotIn("generation_config", body)

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
        self.assertEqual(body["response_format"], {"type": "video", "resolution": "720p"})

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

    async def test_omni_retrieve_preserves_vertex_plural_errors(self):
        data = {
            "id": "v1_blocked",
            "status": "failed",
            "errors": [{
                "code": "content_blocked",
                "message": "The input violates Google's Responsible AI practices.",
            }],
        }
        job = {
            "model": "gemini-omni-1.1-flash",
            "provider_request_id": "v1_blocked",
            "request_metadata": {"upstream_model": "gemini-omni-1.1-flash-preview"},
        }
        adapter = VertexAdapter()
        with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "project-1"}), patch.object(
            adapter, "_vertex_headers", new=AsyncMock(return_value={"Authorization": "Bearer token"})
        ), patch(
            "generation_job_adapters._json_request", new=AsyncMock(return_value=data)
        ):
            status = await adapter.retrieve(job)

        self.assertEqual(status.status, "failed")
        self.assertEqual(status.provider_status, "failed")
        self.assertEqual(status.error_code, "content_blocked")
        self.assertEqual(
            status.error_message,
            "The input violates Google's Responsible AI practices.",
        )

    async def test_omni_content_rejects_oversized_inline_video(self):
        data = {
            "id": "v1_large",
            "status": "completed",
            "steps": [{
                "type": "model_output",
                "content": [{
                    "type": "video",
                    "mime_type": "video/mp4",
                    "data": base64.b64encode(b"12345").decode(),
                }],
            }],
        }
        job = {
            "model": "gemini-omni-1.1-flash",
            "provider_request_id": "v1_large",
            "request_metadata": {"upstream_model": "gemini-omni-1.1-flash-preview"},
        }
        adapter = VertexAdapter()
        with patch.dict(os.environ, {
            "GOOGLE_CLOUD_PROJECT": "project-1",
            "GEMINI_OMNI_MAX_CONTENT_BYTES": "4",
        }), patch.object(
            adapter, "_vertex_headers", new=AsyncMock(return_value={"Authorization": "Bearer token"})
        ), patch("generation_job_adapters._json_request", new=AsyncMock(return_value=data)):
            with self.assertRaises(ProviderAdapterError) as caught:
                await adapter.content(job)
        self.assertEqual(caught.exception.code, "CONTENT_TOO_LARGE")

    async def test_omni_maps_first_and_last_frame_interpolation(self):
        request = GenerationJobCreate(
            model="gemini-omni-1.1-flash",
            prompt="Interpolate",
            operation="generate",
            resolution="1080p",
            duration_seconds=5,
            media_inputs=[
                {"type": "image", "role": "first_frame", "upload_field": "first"},
                {"type": "image", "role": "last_frame", "upload_field": "last"},
            ],
        )
        mocked = AsyncMock(return_value={"id": "v1_interpolation", "status": "in_progress"})
        adapter = VertexAdapter()
        with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "project-1"}), patch.object(
            adapter, "_vertex_headers", new=AsyncMock(return_value={"Authorization": "Bearer token"})
        ), patch("generation_job_adapters._json_request", new=mocked):
            await adapter.submit(
                request,
                job_id="gen_interpolation",
                callback_url=None,
                upload_bytes={
                    "first": ("first.png", b"first", "image/png"),
                    "last": ("last.png", b"last", "image/png"),
                },
            )
        body = mocked.await_args.kwargs["body"]
        prompt = body["input"][0]["content"][-1]["text"]
        self.assertIn("<FIRST_FRAME>@Image1", prompt)
        self.assertIn("<LAST_FRAME>@Image2", prompt)
        self.assertEqual(body["generation_config"]["video_config"]["task"], "image_to_video")
        self.assertEqual(
            body["response_format"],
            {"type": "video", "resolution": "1080p", "duration": "5s", "aspect_ratio": "16:9"},
        )

    async def test_omni_source_extension_accepts_reference_images(self):
        request = GenerationJobCreate(
            model="gemini-omni-flash",
            prompt="Continue into a moonlit clearing",
            operation="extend",
            resolution="4K",
            duration_seconds=3,
            media_inputs=[
                {"type": "video", "role": "source", "upload_field": "source"},
                {"type": "image", "role": "reference", "upload_field": "reference"},
            ],
        )
        mocked = AsyncMock(return_value={"id": "v1_extension", "status": "in_progress"})
        adapter = VertexAdapter()
        with patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "project-1"}), patch.object(
            adapter, "_vertex_headers", new=AsyncMock(return_value={"Authorization": "Bearer token"})
        ), patch("generation_job_adapters._json_request", new=mocked):
            result = await adapter.submit(
                request,
                job_id="gen_extension",
                callback_url=None,
                upload_bytes={
                    "source": ("source.mp4", self._mp4_with_duration(10), "video/mp4"),
                    "reference": ("reference.webp", b"reference", "image/webp"),
                },
            )
        body = mocked.await_args.kwargs["body"]
        self.assertEqual(body["generation_config"]["video_config"]["task"], "extend")
        self.assertEqual(body["response_format"], {"type": "video", "resolution": "4k", "duration": "3s"})
        self.assertEqual(result.request_metadata["upstream_model"], "gemini-omni-1.1-flash-preview")
        self.assertEqual(result.request_metadata["operation"], "extend")

    async def test_omni_rejects_last_frame_without_first_frame(self):
        request = GenerationJobCreate(
            model="gemini-omni-1.1-flash",
            prompt="Interpolate",
            media_inputs=[{"type": "image", "role": "last_frame", "upload_field": "last"}],
        )
        with self.assertRaises(ProviderAdapterError) as caught:
            await VertexAdapter().submit(
                request,
                job_id="gen_invalid",
                callback_url=None,
                upload_bytes={"last": ("last.png", b"last", "image/png")},
            )
        self.assertEqual(caught.exception.code, "INVALID_MEDIA_INPUT")

    async def test_omni_rejects_mismatched_media_roles(self):
        cases = (
            (
                {"type": "image", "role": "source", "upload_field": "media"},
                ("image.png", b"image", "image/png"),
            ),
            (
                {"type": "video", "role": "reference", "upload_field": "media"},
                ("video.mp4", self._mp4_with_duration(10), "video/mp4"),
            ),
        )
        for media_input, upload in cases:
            with self.subTest(media_input=media_input):
                request = GenerationJobCreate(
                    model="gemini-omni-1.1-flash",
                    prompt="Invalid role",
                    operation="edit" if media_input["type"] == "video" else "generate",
                    media_inputs=[media_input],
                )
                with self.assertRaises(ProviderAdapterError) as caught:
                    await VertexAdapter().submit(
                        request,
                        job_id="gen_invalid_role",
                        callback_url=None,
                        upload_bytes={"media": upload},
                    )
                self.assertEqual(caught.exception.code, "INVALID_MEDIA_INPUT")

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

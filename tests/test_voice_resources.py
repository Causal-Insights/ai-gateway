"""Private resource authorization and lifecycle; provider calls are always mocked."""
import json
import io
import os
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException

import voice_resources as voices
from generation_job_repository import GenerationJobRepository


class VoiceAuthTests(unittest.TestCase):
    def test_only_authenticated_delegate_metadata_grants_access(self):
        headers = {"X-Gateway-Resource-Owner": "alice"}
        for metadata in ({}, {"gateway_app_id": "app", "gateway_resource_delegate": "true"}):
            with self.assertRaises(HTTPException):
                voices.owner_for(SimpleNamespace(metadata=metadata), headers)
        delegate = SimpleNamespace(metadata={"gateway_app_id": "app", "gateway_resource_delegate": True})
        self.assertEqual(voices.owner_for(delegate, headers), ("app", "alice"))
        with self.assertRaises(HTTPException):
            voices.owner_for(delegate, {})

    def test_provider_verification_can_transition_to_ready(self):
        self.assertTrue(voices.verification_required({"requires_verification": True}))
        self.assertFalse(voices.verification_required({"voice_verification": {"requires_verification": True, "is_verified": True}}, True))
        self.assertTrue(voices.verification_required({}, True))


class VoiceSdkBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_validation_accepts_real_wav_and_rejects_non_audio(self):
        output = io.BytesIO()
        with wave.open(output, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 1600)
        await voices.validate_sample(output.getvalue())
        with self.assertRaises(HTTPException):
            await voices.validate_sample(b"not an audio file")

    async def test_sdk_overwrites_body_forged_owner_headers(self):
        from litellm.proxy.litellm_pre_call_utils import add_litellm_data_to_request
        from starlette.requests import Request
        user = voices.UserAPIKeyAuth(api_key="test-key", metadata={"gateway_app_id": "app", "gateway_resource_delegate": True})
        request = Request({"type": "http", "method": "POST", "scheme": "http", "server": ("gateway", 80),
            "path": "/v1/audio/speech", "query_string": b"", "headers": [(b"x-gateway-resource-owner", b"alice")]})
        data = {"model": "elevenlabs-v3-tts", "input": "Hello", "voice": "voice:voice_test",
            "proxy_server_request": {"headers": {"x-gateway-resource-owner": "victim"}}}
        result = await add_litellm_data_to_request(data=data, request=request, user_api_key_dict=user,
            proxy_config=MagicMock(), general_settings={})
        self.assertEqual(voices.owner_for(user, result["proxy_server_request"]["headers"]), ("app", "alice"))


@unittest.skipUnless(os.environ.get("RUN_VOICE_DB_TESTS") == "1", "requires disposable PostgreSQL")
class VoiceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = GenerationJobRepository()
        async def migrate_voice_table():
            sql = (Path(__file__).parents[1] / "migrations/005_voice_resources.sql").read_text()
            await self.repo._pool.execute(sql)
            await self.repo._pool.execute(sql)
        self.repo._migrate = migrate_voice_table
        self.repo_patch = patch.object(voices, "repository", self.repo)
        self.repo_patch.start()
        self.key_patch = patch.dict(os.environ, {"ELEVENLABS_API_KEY": "mock-only"})
        self.key_patch.start()
        self.provider = AsyncMock()
        self.provider_patch = patch.object(voices, "provider_request", self.provider)
        self.provider_patch.start()
        self.decode_patch = patch.object(voices, "validate_sample", AsyncMock())
        self.decode_patch.start()
        self.app_id = uuid4().hex
        self.delegate = SimpleNamespace(metadata={"gateway_app_id": self.app_id, "gateway_resource_delegate": True})
        app = FastAPI()
        app.include_router(voices.router)
        app.dependency_overrides[voices.user_api_key_auth] = lambda: self.delegate
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"X-Gateway-Resource-Owner": "alice"})
        self.request_id = uuid4().hex
        self.provider.return_value = httpx.Response(200, json={"voice_id": "provider-" + uuid4().hex, "requires_verification": False})

    async def asyncTearDown(self):
        await self.client.aclose()
        pool = await self.repo.pool()
        await pool.execute("delete from gateway_voice_events where voice_id in (select id from gateway_voice_resources where app_id=$1)", self.app_id)
        await pool.execute("delete from gateway_voice_resources where app_id=$1", self.app_id)
        await self.repo.close()
        for patcher in (self.decode_patch, self.provider_patch, self.key_patch, self.repo_patch):
            patcher.stop()

    async def create(self, name="My voice", **kwargs):
        return await self.client.post("/v1/voices", data={"name": name, "request_id": self.request_id,
            "consent": json.dumps({"version": "voice-cloning-permission-v1", "actor_id": "alice", "accepted_at": "2026-09-24T10:00:00Z"})},
            files=[("files", ("sample.wav", b"mock audio", "audio/wav"))], **kwargs)

    async def test_create_replay_conflict_owner_isolation_and_key_rotation(self):
        first = await self.create()
        self.assertEqual(first.status_code, 201, first.text)
        resource = first.json()
        self.assertEqual(resource["state"], "ready")
        self.assertNotIn("provider_voice_id", resource)
        replay = await self.create()
        self.assertEqual(replay.json()["id"], resource["id"])
        self.assertEqual(sum(call.args[0] == "POST" for call in self.provider.call_args_list), 1)
        self.assertEqual((await self.create(name="Changed")).status_code, 409)
        self.assertEqual((await self.client.get("/v1/voices/" + resource["id"], headers={"X-Gateway-Resource-Owner": "bob"})).status_code, 404)
        self.delegate.metadata = {"gateway_app_id": self.app_id, "gateway_resource_delegate": True, "rotated_key": True}
        own = await self.client.get("/v1/voices", params={"request_id": self.request_id})
        self.assertEqual([row["id"] for row in own.json()["data"]], [resource["id"]])
        self.delegate.metadata["gateway_app_id"] = "another-app"
        self.assertEqual((await self.client.get("/v1/voices")).json()["data"], [])

    async def test_verification_recovery_and_speech_owner_resolution(self):
        upstream_id = "provider-" + uuid4().hex
        self.provider.return_value = httpx.Response(200, json={"voice_id": upstream_id, "requires_verification": True})
        resource = (await self.create()).json()
        data = {"model": "elevenlabs-v3-tts", "voice": "voice:" + resource["id"], "proxy_server_request": {"headers": {"x-gateway-resource-owner": "alice"}}}
        hook = voices.VoiceAccess()
        with self.assertRaises(HTTPException) as failure:
            await hook.async_pre_call_hook(self.delegate, None, data.copy(), "aspeech")
        self.assertEqual(failure.exception.detail["code"], "VOICE_VERIFICATION_REQUIRED")
        self.provider.return_value = httpx.Response(200, json={"voice_id": upstream_id, "voice_verification": {"is_verified": True}})
        self.assertEqual((await self.client.get("/v1/voices/" + resource["id"])).json()["state"], "ready")
        result = await hook.async_pre_call_hook(self.delegate, None, data.copy(), "aspeech")
        self.assertEqual(result["voice"], upstream_id)
        with self.assertRaises(HTTPException):
            await hook.async_pre_call_hook(self.delegate, None, {**data, "voice": upstream_id}, "aspeech")
        with self.assertRaises(HTTPException):
            await hook.async_pre_call_hook(self.delegate, None, {**data, "proxy_server_request": {"headers": {"x-gateway-resource-owner": "bob"}}}, "aspeech")
        curated = {"model": "elevenlabs-v3-tts", "voice": "existing-curated-provider-id"}
        self.assertEqual(await hook.async_pre_call_hook(SimpleNamespace(metadata={}), None, curated, "aspeech"), curated)

    async def test_unknown_creation_reconciles_without_second_post(self):
        self.provider.side_effect = httpx.ReadTimeout("unknown")
        response = await self.create()
        self.assertEqual(response.status_code, 202)
        resource = response.json()
        self.assertEqual(resource["state"], "outcome_unknown")
        self.provider.side_effect = None
        self.provider.return_value = httpx.Response(200, json={"voices": [{"voice_id": "recovered-" + uuid4().hex,
            "labels": {"gateway_resource_id": resource["id"]}, "voice_verification": {"requires_verification": False}}], "has_more": False})
        recovered = await self.client.get("/v1/voices/" + resource["id"])
        self.assertEqual(recovered.json()["state"], "ready")
        self.assertEqual(sum(call.args[0] == "POST" for call in self.provider.call_args_list), 1)

    async def test_unreconciled_private_provider_id_cannot_bypass_ownership(self):
        self.provider.side_effect = httpx.ReadTimeout("unknown")
        resource = (await self.create()).json()
        self.provider.side_effect = None
        provider_id = "guessed-private-" + uuid4().hex
        self.provider.return_value = httpx.Response(200, json={"voice_id": provider_id,
            "labels": {"gateway_resource_id": resource["id"]}})
        with self.assertRaises(HTTPException) as failure:
            await voices.VoiceAccess().async_pre_call_hook(SimpleNamespace(metadata={}), None,
                {"model": "elevenlabs-v3-tts", "voice": provider_id}, "aspeech")
        self.assertEqual(failure.exception.detail["code"], "VOICE_RESOURCE_ID_REQUIRED")
        self.assertEqual((await voices.owned_row(resource["id"], (self.app_id, "alice")))["provider_voice_id"], provider_id)

    async def test_delete_revokes_before_provider_cleanup_and_retries(self):
        resource = (await self.create()).json()
        self.provider.side_effect = httpx.ReadTimeout("offline")
        deleted = await self.client.delete("/v1/voices/" + resource["id"])
        self.assertEqual(deleted.status_code, 202)
        self.assertTrue(deleted.json()["cleanup_pending"])
        data = {"model": "elevenlabs-v3-tts", "voice": "voice:" + resource["id"], "proxy_server_request": {"headers": {"x-gateway-resource-owner": "alice"}}}
        with self.assertRaises(HTTPException):
            await voices.VoiceAccess().async_pre_call_hook(self.delegate, None, data, "aspeech")
        self.provider.side_effect = None
        deleted = await self.client.delete("/v1/voices/" + resource["id"])
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(deleted.json()["cleanup_pending"])
        self.assertEqual(deleted.json()["state"], "deleted")

    async def test_definitive_rejection_can_be_deleted_without_unreachable_cleanup(self):
        self.provider.return_value = httpx.Response(400, json={"detail": "invalid"})
        resource = (await self.create()).json()
        self.assertEqual(resource["state"], "failed")
        self.provider.reset_mock()
        deleted = await self.client.delete("/v1/voices/" + resource["id"])
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(deleted.json()["cleanup_pending"])
        self.provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()

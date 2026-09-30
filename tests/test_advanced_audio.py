"""Advanced audio routes use disposable PostgreSQL and mocked provider/accounting."""
import asyncio
import base64
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request

import advanced_audio as audio
import voice_resources as voices
from generation_job_repository import GenerationJobRepository
from pricing_registry import registry, PricingError


class AudioContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_plan_and_seed_combinations_preserve_model_identity(self):
        v1 = {"positive_global_styles": ["jazz"], "negative_global_styles": [], "sections": [
            {"section_name": "Intro", "duration_ms": 6000, "positive_local_styles": [], "negative_local_styles": [], "lines": []}]}
        prepared = await audio.prepare_music({"model": "elevenlabs-music", "composition_plan": v1, "seed": 0}, ("app", "owner"))
        self.assertEqual(prepared, {"model_id": "music_v1", "composition_plan": v1, "seed": 0})
        for request in ({"prompt": "jazz", "composition_plan": v1}, {"prompt": "jazz", "seed": 1}, {"composition_plan": {"chunks": []}}):
            with self.assertRaises(HTTPException):
                await audio.prepare_music({"model": "elevenlabs-music", **request}, ("app", "owner"))

    async def test_pcm_is_playable_lossless_wav_without_25mb_output_discard(self):
        pcm = b"\0\0" * (14 * 1024 * 1024)
        with patch.object(audio, "validate_sample", AsyncMock()):
            data, mime, _ = await audio.audio_bytes(httpx.Response(200, content=pcm), "pcm_44100")
        self.assertEqual(mime, "audio/wav")
        self.assertEqual(data[44:], pcm)

    def test_free_plan_receipt_is_explicit_zero_and_not_a_generation_tariff(self):
        profile = audio.free_plan_profile("elevenlabs-music-2.5")
        result = registry().calculate(profile, {"requests": 1}, served_model="music_v2_5", zero_reason="provider_documented_free")
        self.assertEqual(result["cost_usd"], "0")
        self.assertEqual(result["zero_reason"], "provider_documented_free")

    async def test_timestamp_flag_routes_additively_and_default_speech_stays_native(self):
        seen = []
        async def app(scope, receive, send):
            seen.append((scope["path"], await receive()))
        middleware = audio.SpeechOptionsMiddleware(app)
        for payload in ({"model": "elevenlabs-v3-tts"}, {"with_timestamps": True}):
            message = {"type": "http.request", "body": json.dumps(payload).encode()}
            await middleware({"type": "http", "method": "POST", "path": "/v1/audio/speech"}, AsyncMock(return_value=message), AsyncMock())
        self.assertEqual([item[0] for item in seen], ["/v1/audio/speech", "/v1/audio/speech-with-timestamps"])


@unittest.skipUnless(os.environ.get("RUN_VOICE_DB_TESTS") == "1", "requires disposable PostgreSQL")
class AudioLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = GenerationJobRepository()
        async def migrate():
            for name in ("005_voice_resources.sql", "006_advanced_audio.sql"):
                await self.repo._pool.execute((Path(__file__).parents[1] / "migrations" / name).read_text())
        self.repo._migrate = migrate
        self.app_id = uuid4().hex
        self.user = SimpleNamespace(api_key="mock-key", metadata={"gateway_app_id": self.app_id, "gateway_resource_delegate": True}, budget_reservation=None)
        self.provider = AsyncMock(return_value=httpx.Response(200, content=b"audio-fixture", headers={"request-id": "upstream"}))
        self.accounting = SimpleNamespace(check_budgets=AsyncMock(), begin=AsyncMock(side_effect=lambda **kw: kw["accounting_id"]),
            attempt=AsyncMock(), observe=AsyncMock(), finish=AsyncMock())
        self.patches = [patch.object(audio, "repository", self.repo), patch.object(voices, "repository", self.repo),
            patch.object(audio, "provider_request", self.provider), patch.object(audio, "accounting", self.accounting),
            patch.object(audio, "finalize_reservation", AsyncMock()), patch.object(audio, "validate_sample", AsyncMock()),
            patch.object(audio, "media_duration_ms", AsyncMock(return_value=6000))]
        for patcher in self.patches:
            patcher.start()
        app = FastAPI()
        app.include_router(audio.router)
        app.dependency_overrides[audio.user_api_key_auth] = lambda: self.user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", headers={"X-Gateway-Resource-Owner": "alice"})

    async def asyncTearDown(self):
        await self.client.aclose()
        pool = await self.repo.pool()
        await pool.execute("delete from gateway_speech_sessions where id in (select id from gateway_audio_requests where app_id=$1)", self.app_id)
        await pool.execute("delete from gateway_song_resources where app_id=$1", self.app_id)
        await pool.execute("delete from gateway_audio_payloads where request_id in (select id from gateway_audio_requests where app_id=$1)", self.app_id)
        await pool.execute("delete from gateway_audio_requests where app_id=$1", self.app_id)
        await self.repo.close()
        for patcher in reversed(self.patches):
            patcher.stop()

    async def test_dialogue_persists_alignment_and_replay_does_not_resubmit(self):
        self.provider.return_value = httpx.Response(200, json={"audio_base64": base64.b64encode(b"audio-fixture").decode(),
            "alignment": {"characters": ["H"], "character_start_times_seconds": [0], "character_end_times_seconds": [.2]}}, headers={"character-cost": "8"})
        request_id = uuid4().hex
        payload = {"model": "elevenlabs-v3-tts", "inputs": [{"text": "Hi", "voice": "curated-a"}, {"text": "Hello!", "voice": "curated-b"}], "with_timestamps": True}
        first = await self.client.post("/v1/audio/dialogue", json=payload, headers={"Idempotency-Key": request_id})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.provider.call_args.args[:2], ("POST", "/v1/text-to-dialogue/with-timestamps"))
        self.assertEqual(self.provider.call_args.kwargs["json"]["inputs"][1]["voice_id"], "curated-b")
        self.assertEqual(self.accounting.observe.call_args.kwargs["raw_usage"], {"billable_characters": "8"})
        replay = await self.client.post("/v1/audio/dialogue", json=payload, headers={"Idempotency-Key": request_id})
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(self.provider.await_count, 1)
        other = await self.client.get("/v1/audio/requests/" + request_id, headers={"X-Gateway-Resource-Owner": "bob"})
        self.assertEqual(other.status_code, 404)
        payload["inputs"][0]["text"] = "Different"
        self.assertEqual((await self.client.post("/v1/audio/dialogue", json=payload, headers={"Idempotency-Key": request_id})).status_code, 409)

    async def test_owner_erasure_revokes_replay_and_prevents_late_output_resurrection(self):
        request_id = uuid4().hex
        response = await self.client.post("/v1/audio/dialogue", json={"model": "elevenlabs-v3-tts", "inputs": [{"text": "Hello", "voice": "curated"}], "request_id": request_id})
        self.assertEqual(response.status_code, 200, response.text)
        pool = await self.repo.pool()
        row = await pool.fetchrow("select * from gateway_audio_requests where request_id=$1", request_id)
        await self.client.delete("/v1/audio/owner-data", headers={"X-Gateway-Resource-Owner": "bob"})
        self.assertEqual((await self.client.get("/v1/audio/requests/" + request_id)).status_code, 200)
        deleted = await self.client.delete("/v1/audio/owner-data")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(await pool.fetchval("select count(*) from gateway_audio_payloads where request_id=$1", row["id"]), 0)
        with self.assertRaises(HTTPException):
            await audio.finish_operation(row, {}, b"late-output", "audio/mpeg")
        self.assertEqual((await self.client.get("/v1/audio/requests/" + request_id)).status_code, 409)

    async def test_timeout_keeps_unknown_without_paid_retry(self):
        self.provider.side_effect = httpx.ReadTimeout("uncertain")
        payload = {"model": "elevenlabs-v3-tts", "input": "Hello", "voice": "curated", "request_id": uuid4().hex}
        response = await self.client.post("/v1/audio/speech-with-timestamps", json=payload)
        self.assertEqual(response.status_code, 504)
        replay = await self.client.post("/v1/audio/speech-with-timestamps", json=payload)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["state"], "outcome_unknown")
        self.assertEqual(self.provider.await_count, 1)

    async def test_plan_is_free_and_sfx_controls_reach_exact_endpoint(self):
        plan = {"chunks": [{"text": "[Intro]", "duration_ms": 6000, "positive_styles": ["jazz"]}]}
        self.provider.return_value = httpx.Response(200, json=plan)
        response = await self.client.post("/v1/audio/music/plan", json={"model": "elevenlabs-music-2.5", "prompt": "Jazz", "music_length_ms": 6000})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["composition_plan"], plan)
        self.assertEqual(self.accounting.observe.call_args.kwargs["zero_reason"], "provider_documented_free")
        self.provider.return_value = httpx.Response(200, content=b"audio")
        response = await self.client.post("/v1/audio/generations", json={"model": "elevenlabs-sfx", "prompt": "Wind", "duration_seconds": .5, "loop": True, "prompt_influence": 0, "output_format": "mp3_44100_128"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.provider.call_args.args[1], "/v1/sound-generation")
        self.assertIs(self.provider.call_args.kwargs["json"]["loop"], True)
        self.assertEqual(self.provider.call_args.kwargs["json"]["prompt_influence"], 0)

    async def test_header_only_song_import_replays_once_and_rejects_other_owner(self):
        self.provider.return_value = httpx.Response(200, json={"song_id": "provider-import"})
        request_id = uuid4().hex
        for _ in range(2):
            response = await self.client.post("/v1/songs", headers={"Idempotency-Key": request_id}, files={"file": ("source.wav", b"audio", "audio/wav")})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.provider.await_count, 1)
        self.assertEqual(response.json()["request_id"], request_id)
        song_id = response.json()["song_id"]
        self.assertEqual((await self.client.get(f"/v1/songs/{song_id}", headers={"X-Gateway-Resource-Owner": "bob"})).status_code, 404)
        invalid = await self.client.post(f"/v1/songs/{song_id}/takes", json={"model": "elevenlabs-music-2.5", "operation": "edit", "chunks": ["invalid"]})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.provider.await_count, 1)

    async def test_invalid_output_is_terminal_and_never_resubmitted(self):
        self.provider.return_value = httpx.Response(200, content=b"")
        request_id = uuid4().hex
        payload = {"model": "elevenlabs-sfx", "prompt": "wind"}
        response = await self.client.post("/v1/audio/generations", json=payload, headers={"Idempotency-Key": request_id})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"]["code"], "AUDIO_OUTPUT_INVALID")
        replay = await self.client.post("/v1/audio/generations", json=payload, headers={"Idempotency-Key": request_id})
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json()["state"], "failed")
        self.assertEqual(self.provider.await_count, 1)
        self.assertEqual(self.accounting.observe.call_args.kwargs["outcome"], "unknown")

    async def test_malformed_import_response_keeps_unknown_without_retry(self):
        self.provider.return_value = httpx.Response(200, content=b"not-json")
        request_id = uuid4().hex
        first = await self.client.post("/v1/songs", headers={"Idempotency-Key": request_id}, files={"file": ("source.wav", b"audio", "audio/wav")})
        self.assertEqual(first.status_code, 502)
        second = await self.client.post("/v1/songs", headers={"Idempotency-Key": request_id}, files={"file": ("source.wav", b"audio", "audio/wav")})
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json()["state"], "outcome_unknown")
        self.assertEqual(self.provider.await_count, 1)
        self.assertEqual(self.accounting.observe.call_args.kwargs["outcome"], "unknown")

    async def test_invalid_free_plan_finishes_its_receipt_without_remaining_pending(self):
        self.provider.return_value = httpx.Response(200, json={"chunks": "invalid"})
        request_id = uuid4().hex
        payload = {"model": "elevenlabs-music-2.5", "prompt": "jazz", "request_id": request_id}
        response = await self.client.post("/v1/audio/music/plan", json=payload)
        self.assertEqual(response.status_code, 502)
        replay = await self.client.post("/v1/audio/music/plan", json=payload)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(self.provider.await_count, 1)
        self.assertEqual(self.accounting.observe.call_args.kwargs["zero_reason"], "provider_documented_free")

    async def test_new_music_remains_blocked_by_unverified_pricing(self):
        with patch.object(audio, "registry", return_value=SimpleNamespace(select=MagicMock(side_effect=PricingError("Unverified test profile")))):
            response = await self.client.post("/v1/audio/generations", json={"model": "elevenlabs-music-2.5", "prompt": "Jazz", "music_length_ms": 3000})
        self.assertEqual(response.status_code, 503, response.text)
        self.provider.assert_not_called()

    async def test_stream_ticket_is_one_use_and_complete_audio_is_persisted(self):
        ticket = await self.client.post("/v1/audio/sessions", json={"model": "elevenlabs-v3-tts", "voice": "curated-a", "max_characters": 5})
        self.assertEqual(ticket.status_code, 200, ticket.text)
        created = ticket.json()
        token = created["stream_path"].split("session_token=")[1]
        self.assertIsNone(await audio.claim_session(created["id"], "wrong-token"))
        class Client:
            query_params = {"session_token": token}
            scope = {"query_string": b"redacted-after-auth"}
            messages = [{"type": "text", "text": "Hello"}, {"type": "close"}]
            events = []
            async def accept(self): pass
            async def receive_json(self): return self.messages.pop(0)
            async def send_json(self, event): self.events.append(event)
            async def close(self, code): self.closed = code
        class Provider:
            def __init__(self): self.sent = []; self.closed = asyncio.Event()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def send(self, text):
                message = json.loads(text); self.sent.append(message)
                if message.get("close_socket"): self.closed.set()
            def __aiter__(self): return self.responses()
            async def responses(self):
                await self.closed.wait()
                yield json.dumps({"audio": base64.b64encode(b"streamed-audio").decode(), "alignment": {"characters": ["H"]}})
                yield json.dumps({"is_final": True})
        client, provider = Client(), Provider()
        with patch.object(audio, "websocket_connect", return_value=provider), patch.dict(os.environ, {"ELEVENLABS_API_KEY": "mock-private-key"}):
            await audio.speech_stream(client, created["id"])
        self.assertEqual(client.closed, 1000)
        self.assertEqual(provider.sent[0], {"voices": ["curated-a"]})
        self.assertEqual(provider.sent[1]["inputs"][0]["text"], "Hello")
        self.assertIsNone(await audio.claim_session(created["id"], token))
        self.assertEqual(client.scope["query_string"], b"")
        self.assertNotIn("mock-private-key", json.dumps(client.events))
        saved = await self.client.get("/v1/audio/requests/" + created["id"])
        self.assertEqual(base64.b64decode(saved.json()["audio_base64"]), b"streamed-audio")
        self.assertEqual(self.accounting.observe.call_args.kwargs["raw_usage"], {"billable_characters": 5})

    async def test_stream_scope_rejects_new_voice_and_excess_characters(self):
        for message in ({"type": "text", "text": "Hello!"}, {"type": "text", "text": "Hi", "voice": "foreign"}):
            with self.assertRaises(HTTPException):
                audio.websocket_input(message, model="elevenlabs-v3-tts", voices={"owned": "provider"}, characters=0, limit=5)
        payload, count, closing = audio.websocket_input({"type": "text", "text": "Hi", "model": "forged", "xi_api_key": "forged"}, model="elevenlabs-multilingual-v2", voices={"owned": "provider"}, characters=0, limit=5)
        self.assertEqual(payload, {"text": "Hi", "try_trigger_generation": True})
        self.assertEqual(count, 2)
        self.assertFalse(closing)

    async def test_song_nested_reference_ownership_and_local_deletion(self):
        # Existing generation creates the source through the same ordinary route.
        self.provider.return_value = httpx.Response(200, content=b"audio", headers={"song-id": "provider-song"})
        first = await self.client.post("/v1/audio/generations", json={"model": "elevenlabs-music", "prompt": "Jazz", "music_length_ms": 6000})
        self.assertEqual(first.status_code, 200, first.text)
        song_id = first.json()["song_id"]
        plan = {"chunks": [{"text": "[Chorus]", "duration_ms": 6000, "conditioning_ref": {"song_id": song_id, "range": {"start_ms": 0, "end_ms": 1000}}}]}
        resolved = await audio.resolve_plan(plan, "elevenlabs-music-2.5", (self.app_id, "alice"))
        self.assertEqual(resolved["chunks"][0]["conditioning_ref"]["song_id"], "provider-song")
        self.assertEqual(plan["chunks"][0]["conditioning_ref"]["song_id"], song_id)
        with self.assertRaises(HTTPException):
            await audio.resolve_plan(plan, "elevenlabs-music-2.5", (self.app_id, "bob"))
        content = await self.client.get(f"/v1/songs/{song_id}/content")
        self.assertEqual(content.content, b"audio")
        self.assertEqual((await self.client.get(f"/v1/songs/{song_id}/content", headers={"X-Gateway-Resource-Owner": "bob"})).status_code, 404)
        deleted = await self.client.delete(f"/v1/songs/{song_id}")
        self.assertFalse(deleted.json()["provider_cleanup_supported"])
        self.assertEqual((await self.client.get(f"/v1/songs/{song_id}/content")).status_code, 404)


if __name__ == "__main__":
    unittest.main()

import copy
import json
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException, Response
from litellm.proxy._types import UserAPIKeyAuth
from generation_job_models import GenerationJobCreateV2
from generation_job_adapters import XAIAdapter, ProviderAdapterError
from generation_job_routes import create_generation_job, get_generation_job, _hash_request_v2
from grok_video_contract import validate_grok_video_v2, ADAPTER_REVISION
from tests.test_generation_jobs import callback_request

FIXTURES = json.loads((Path(__file__).parent / "fixtures/generation_jobs_v2/grok_video_15_profiles.json").read_text())

class GrokV2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_every_magiclens_profile_matches_exact_xai_request(self):
        for fixture in FIXTURES:
            body = fixture["body"]
            request = GenerationJobCreateV2.model_validate(body)
            validate_grok_video_v2(request)
            with patch.dict(os.environ, {"GROK_API_KEY": "test-only"}), patch("generation_job_adapters._json_request", new_callable=AsyncMock, return_value={"request_id":"original-job"}) as post, patch.object(XAIAdapter, "_upload_file", new_callable=AsyncMock, return_value={"file_id":"first-frame-file"}):
                result = await XAIAdapter().submit(request, job_id="test-job", callback_url=None)
            self.assertEqual(result.provider_request_id,"original-job")
            self.assertEqual(post.await_count,1)
            self.assertEqual(post.await_args.args[1],"https://api.x.ai/v1/videos/generations")
            wire = post.await_args.kwargs["body"]
            self.assertEqual(wire["model"],"grok-imagine-video-1.5")
            self.assertEqual(wire["duration"],1)
            self.assertEqual(wire["resolution"],"480p")
            self.assertIs(wire["generate_audio"],True)
            self.assertEqual(wire["reference_audios"],[{"voice_id":"eve"}])
            if fixture["name"] == "first_frame":
                self.assertEqual(wire["image"],{"file_id":"first-frame-file"})
                self.assertNotIn("aspect_ratio",wire)
                self.assertNotIn("reference_images",wire)
            else:
                self.assertEqual(wire["aspect_ratio"],"16:9")
            if fixture["name"] == "references":
                self.assertEqual(wire["reference_images"],[{"url":x["url"]} for x in body["media"]])

    async def test_invalid_contract_never_creates_a_job_or_calls_provider(self):
        original=FIXTURES[0]["body"]
        invalid=[]
        for key,value in [("contract_revision","unknown"),("profile_id","edit.source_video"),("operation","edit")]:
            invalid.append({**original,key:value})
        for key,value in [("duration",0),("duration",1.5),("duration",16),("resolution","4K"),("resolution","1080p"),("generateAudio",False),("outputCount",2),("frameRate","24fps")]:
            invalid.append({**original,"settings":{**original["settings"],key:value}})
        invalid.append({**original,"voice_ids":["unknown"]})
        for key,value in [("role","last_frame"),("slot_id","firstFrame"),("index",1),("kind","audio")]:
            item=copy.deepcopy(FIXTURES[2]["body"])
            item["media"][0][key]=value
            invalid.append(item)
        for body in invalid:
            with self.subTest(body=body), patch("generation_job_routes.repository.create_or_get",new_callable=AsyncMock) as create, patch("generation_job_adapters._json_request",new_callable=AsyncMock) as provider:
                request=callback_request(json.dumps(body).encode(),[(b"content-type",b"application/json")])
                with self.assertRaises(HTTPException) as error:
                    await create_generation_job(request,Response(),"invalid-fixture",UserAPIKeyAuth(api_key="test-key"))
                self.assertEqual(error.exception.status_code,422)
                create.assert_not_awaited()
                provider.assert_not_awaited()

    async def test_adapter_also_rejects_invalid_profile_before_any_network(self):
        body=copy.deepcopy(FIXTURES[2]["body"])
        body["media"][0]["role"]="first_frame"
        with patch("generation_job_adapters._json_request",new_callable=AsyncMock) as post, patch.object(XAIAdapter,"_upload_file",new_callable=AsyncMock) as upload:
            with self.assertRaises(ProviderAdapterError):
                await XAIAdapter().submit(GenerationJobCreateV2.model_validate(body),job_id="test",callback_url=None)
            post.assert_not_awaited();upload.assert_not_awaited()

    def test_every_semantic_change_changes_the_idempotency_hash(self):
        body=copy.deepcopy(FIXTURES[2]["body"])
        base=_hash_request_v2(GenerationJobCreateV2.model_validate(body),{})
        for key,value in [("voice_ids",["leo"]),("profile_id","generate.first_frame"),("contract_revision","another")]:
            altered={**body,key:value}
            self.assertNotEqual(base,_hash_request_v2(GenerationJobCreateV2.model_validate(altered),{}))
        altered=copy.deepcopy(body);altered["settings"]["duration"]=2
        self.assertNotEqual(base,_hash_request_v2(GenerationJobCreateV2.model_validate(altered),{}))

    async def test_replay_retains_job_identity_and_exact_adapter_without_resubmission(self):
        body = FIXTURES[0]["body"]
        user = UserAPIKeyAuth(api_key="original-owner")
        retained = {"id": "gen_original", "provider_request_id": "xai_original"}
        with patch.dict(os.environ, {"GATEWAY_PUBLIC_BASE_URL": "http://localhost"}), patch("generation_job_routes.repository.create_or_get", new_callable=AsyncMock, return_value=(retained, False, False)) as create, patch("generation_job_routes.adapter_for_job") as adapter, patch("generation_job_routes._response", return_value=retained):
            for _ in range(2):
                request = callback_request(json.dumps(body).encode(), [(b"content-type", b"application/json")])
                result = await create_generation_job(request, Response(), "original-attempt", user)
                self.assertEqual(result, retained)
            adapter.assert_not_called()
            first, second = [call.kwargs for call in create.await_args_list]
            for field in ("owner_key_hash", "request_hash", "idempotency_key", "provider_route", "adapter_revision"):
                self.assertEqual(first[field], second[field])
            self.assertEqual(first["adapter_revision"], ADAPTER_REVISION)
            self.assertEqual(first["provider_route"], "xai_videos_v2")
            self.assertEqual(first["request_metadata"]["contract_revision"], body["contract_revision"])
            self.assertEqual(first["request_metadata"]["profile_id"], body["profile_id"])
            create.return_value = (retained, False, True)
            request = callback_request(json.dumps(body).encode(), [(b"content-type", b"application/json")])
            with self.assertRaises(HTTPException) as conflict:
                await create_generation_job(request, Response(), "original-attempt", user)
            self.assertEqual(conflict.exception.status_code, 409)
            adapter.assert_not_called()

    async def test_retrieval_uses_owner_scope_and_never_submits(self):
        with patch("generation_job_routes.repository.get", new_callable=AsyncMock, return_value=None) as get, patch("generation_job_routes.adapter_for_job") as adapter:
            with self.assertRaises(HTTPException) as denied:
                await get_generation_job("gen_original", callback_request(b""), UserAPIKeyAuth(api_key="different-owner"))
            self.assertEqual(denied.exception.status_code, 404)
            self.assertEqual(get.await_args.args[0], "gen_original")
            self.assertTrue(get.await_args.args[1])
            adapter.assert_not_called()

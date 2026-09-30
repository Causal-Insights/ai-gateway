import copy
import json
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException, Response
from litellm.proxy._types import UserAPIKeyAuth
from generation_job_models import GenerationJobCreateV2
from generation_job_adapters import BytePlusAdapter, ProviderAdapterError
from generation_job_routes import create_generation_job, _hash_request_v2
from seedance_video_contract import MODELS, ADAPTER_REVISION, source_resolution
from tests.test_generation_jobs import callback_request

FIXTURES = json.loads((Path(__file__).parent / "fixtures/generation_jobs_v2/seedance_20_profiles.json").read_text())
PROBE = {"streams": [{"codec_type": "video", "width": 1280, "height": 720, "duration": "5"}]}

class SeedanceV2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_all_eight_profiles_compile_to_exact_provider_requests(self):
        for fixture in FIXTURES:
            body=fixture["body"]
            with self.subTest(model=body["model"],profile=body["profile_id"]), patch.dict(os.environ,{"BYTEDANCE_API_KEY":"test-only"}), patch("generation_job_adapters._json_request",new_callable=AsyncMock,return_value={"id":"original-task"}) as post, patch("generation_job_adapters._download_media",new_callable=AsyncMock,return_value=("source.mp4",b"test-video","video/mp4")) as download, patch("generation_job_adapters.probe_media_bytes",return_value=PROBE):
                result=await BytePlusAdapter().submit(GenerationJobCreateV2.model_validate(body),job_id="stable-job",callback_url="https://gateway.example.test/callback")
                self.assertEqual(result.provider_request_id,"original-task")
                self.assertEqual(post.await_count,1)
                self.assertEqual(post.await_args.args[1],"https://ark.ap-southeast.bytepluses.com/api/v3/contents/generations/tasks")
                wire=post.await_args.kwargs["body"]
                self.assertEqual(wire["model"],MODELS[body["model"]])
                self.assertEqual(wire["duration"],4)
                self.assertIs(wire["generate_audio"],False)
                self.assertEqual(wire["resolution"],"720p")
                self.assertEqual(wire["ratio"],"adaptive" if body["operation"]=="edit" else "16:9")
                self.assertEqual(wire["content"][0],{"type":"text","text":body["prompt"]})
                for actual,item in zip(wire["content"][1:],body["media"],strict=True):
                    role="reference_video" if item["kind"]=="video" else "reference_image" if item["role"]=="reference" else "first_frame"
                    self.assertEqual(actual,{"type":item["kind"]+"_url",item["kind"]+"_url":{"url":item["url"]},"role":role})
                self.assertEqual(download.await_count,1 if body["operation"]=="edit" else 0)
                self.assertEqual(result.request_metadata["resolution"],"720p")

    async def test_invalid_requests_never_create_jobs_or_submit(self):
        for fixture in FIXTURES:
            body=fixture["body"]
            invalid=[{**body,"contract_revision":"unknown"},{**body,"voice_ids":["eve"]},{**body,"operation":"extend"}]
            for key,value in [("duration",3),("duration",4.5),("duration",16),("generateAudio","false"),("outputCount",2),("resolution","4k"),("frameRate",24)]:
                invalid.append({**body,"settings":{**body["settings"],key:value}})
            for key,value in [("slot_id","unknown"),("role","last_frame"),("index",1),("kind","audio")]:
                if body["media"]:
                    changed=copy.deepcopy(body);changed["media"][0][key]=value;invalid.append(changed)
            if body["model"].endswith("fast"):
                invalid.append({**body,"settings":{**body["settings"],"resolution":"1080p"}})
            for item in invalid:
                with patch("generation_job_routes.repository.create_or_get",new_callable=AsyncMock) as create, patch("generation_job_adapters._json_request",new_callable=AsyncMock) as post:
                    request=callback_request(json.dumps(item).encode(),[(b"content-type",b"application/json")])
                    with self.assertRaises(HTTPException) as rejected:
                        await create_generation_job(request,Response(),"test-attempt",UserAPIKeyAuth(api_key="test-owner"))
                    self.assertEqual(rejected.exception.status_code,422)
                    create.assert_not_awaited();post.assert_not_awaited()

    async def test_bad_sources_are_rejected_before_paid_submission(self):
        fixture=next(x for x in FIXTURES if x["profile"]=="edit.source_video" and x["modelKey"].endswith("fast"))
        for probe in [{},{"streams":[{"codec_type":"video","width":1280,"height":720,"duration":16}]},{"streams":[{"codec_type":"video","width":1920,"height":1080,"duration":5}]}]:
            with patch("generation_job_adapters._download_media",new_callable=AsyncMock,return_value=("source.mp4",b"video","video/mp4")), patch("generation_job_adapters.probe_media_bytes",return_value=probe), patch("generation_job_adapters._json_request",new_callable=AsyncMock) as post:
                with self.assertRaises(ProviderAdapterError) as rejected:
                    await BytePlusAdapter().submit(GenerationJobCreateV2.model_validate(fixture["body"]),job_id="test",callback_url=None)
                self.assertEqual(rejected.exception.code,"INVALID_MEDIA_INPUT")
                post.assert_not_awaited()
        for dimensions in [(1280,720),(720,1280),(960,960),(1470,630)]:
            probe={"streams":[{"codec_type":"video","width":dimensions[0],"height":dimensions[1],"duration":5}]}
            self.assertEqual(source_resolution("seedance-2.0",probe),"720p")

    async def test_ambiguous_submission_is_never_retried(self):
        with patch.dict(os.environ,{"BYTEDANCE_API_KEY":"test-only"}), patch("generation_job_adapters._json_request",new_callable=AsyncMock,return_value={}) as post:
            with self.assertRaises(ProviderAdapterError) as unknown:
                await BytePlusAdapter().submit(GenerationJobCreateV2.model_validate(FIXTURES[0]["body"]),job_id="test",callback_url=None)
            self.assertTrue(unknown.exception.outcome_unknown)
            self.assertEqual(post.await_count,1)

    async def test_replay_keeps_exact_revision_and_does_not_resubmit(self):
        for fixture in FIXTURES:
            body=fixture["body"]
            retained={"id":"original-job","provider_request_id":"original-task"}
            with patch.dict(os.environ,{"GATEWAY_PUBLIC_BASE_URL":"http://localhost"}), patch("generation_job_routes.repository.create_or_get",new_callable=AsyncMock,return_value=(retained,False,False)) as create, patch("generation_job_routes.adapter_for_job") as adapter, patch("generation_job_routes._response",return_value=retained):
                for _ in range(2):
                    request=callback_request(json.dumps(body).encode(),[(b"content-type",b"application/json")])
                    result=await create_generation_job(request,Response(),"same-attempt",UserAPIKeyAuth(api_key="same-owner"))
                    self.assertEqual(result,retained)
                first,second=[call.kwargs for call in create.await_args_list]
                for key in ["request_hash","owner_key_hash","idempotency_key","request_metadata"]:self.assertEqual(first[key],second[key])
                self.assertEqual(first["adapter_revision"],ADAPTER_REVISION)
                self.assertEqual(first["provider_route"],"byteplus_ark_v3")
                adapter.assert_not_called()
                changed=copy.deepcopy(body);changed["settings"]["duration"]=5
                self.assertNotEqual(_hash_request_v2(GenerationJobCreateV2.model_validate(body),{}),_hash_request_v2(GenerationJobCreateV2.model_validate(changed),{}))

    async def test_polling_normalizes_terminal_evidence_and_1080p_cost(self):
        job={"provider_request_id":"original-task","model":"seedance-2.0","request_metadata":{"upstream_model":MODELS["seedance-2.0"],"resolution":"1080p","has_input_video":False}}
        responses=[{"status":"queued"},{"status":"running"},{"status":"succeeded","content":{"video_url":"https://assets.example.test/result.mp4"},"usage":{"completion_tokens":100000}},{"status":"failed","error":{"code":"InvalidParameter","message":"invalid"}}]
        with patch.dict(os.environ,{"BYTEDANCE_API_KEY":"test-only"}), patch("generation_job_adapters._json_request",new_callable=AsyncMock,side_effect=responses) as get:
            statuses=[await BytePlusAdapter().retrieve(job) for _ in responses]
            self.assertEqual([s.status for s in statuses],["queued","in_progress","completed","failed"])
            self.assertIsNone(statuses[2].cost_usd)
            self.assertEqual(statuses[2].usage, {"completion_tokens":100000})
            self.assertEqual(statuses[2].result_url,"https://assets.example.test/result.mp4")
            self.assertEqual(statuses[3].error_code,"InvalidParameter")
            self.assertTrue(all(call.args[0]=="GET" and call.args[1].endswith("/original-task") for call in get.await_args_list))

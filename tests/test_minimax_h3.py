"""Hosted H3 mapping, validation, lifecycle and cost regressions (no paid calls)."""
import base64
import json
import os
import struct
import subprocess
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException, Response
from litellm.proxy._types import UserAPIKeyAuth
from starlette.requests import Request

from accounting_usage import extract
from capability_discovery import video_contract
from generation_job_adapters import ProviderAdapterError, adapter_for_job, provider_for_model, route_for
from generation_job_models import GenerationJobCreateV2
from generation_job_routes import create_generation_job, get_generation_job_content, get_generation_job_output, poll_generation_job, _hash_request_v2
from generation_job_scheduler import next_poll_time
from gateway_request_policy import apply_request_policy
from gateway_accounting import options_for
from minimax_video_adapter import MiniMaxAdapter, inspect_media
from minimax_video_contract import PROFILES, validate
from pricing_registry import PricingRegistry, PricingError
from video_capabilities import REVISION, validate as validate_video


def media(kind='image', role='reference', index=0, **kw):
    return dict(slot_id=role, index=index, kind=kind, role=role,
                **(kw or {'url': f'https://assets.example/{kind}-{index}'}))


def request(profile='generate.text', **kw):
    return GenerationJobCreateV2(request_schema_version=2, model=kw.pop('model', 'minimax-h3'),
        contract_revision=REVISION, profile_id=profile, operation=kw.pop('operation', 'generate'),
        prompt=kw.pop('prompt', 'Waves and surf sounds'), **kw)


def http_request():
    return Request({'type': 'http', 'method': 'POST', 'path': '/v1/generation-jobs',
        'scheme': 'http', 'server': ('gateway.example', 80), 'headers': [], 'query_string': b''})


def job(**kw):
    now = datetime.now(timezone.utc)
    return dict(id='gen_h3', modality='video', model='minimax-h3', provider='minimax',
        provider_route='minimax_h3_v2', adapter_revision='minimax_h3_v2@2026-10-02',
        provider_request_id='task-h3', created_at=now, updated_at=now, deadline_at=now+timedelta(hours=2),
        request_metadata={'upstream_model': 'MiniMax-H3', 'resolution': '768p'}, **kw)


def receipt(status='succeeded', **kw):
    return {'task': dict(id='task-h3', model='MiniMax-H3', status=status, resolution='768P', duration=4,
        content={'url': 'https://cdn.example/video.mp4'},
        usage={'output_seconds': 4, 'input_seconds': 0, 'input_image_count': 0}, **kw)}


class ContractTests(unittest.TestCase):
    def test_catalog_and_pricing_identify_exact_hosted_model(self):
        import yaml
        config = yaml.safe_load((Path(__file__).parents[1] / 'litellm_config.yaml').read_text())
        models = {item['model_name']: item for item in config['model_list']}
        for alias in ('minimax-h3', 'MiniMax-H3'):
            self.assertEqual(models[alias]['litellm_params']['model'], 'minimax/MiniMax-H3')
            self.assertEqual(models[alias]['litellm_params']['api_key'], 'os.environ/MINIMAX_API_KEY')
            self.assertEqual(PricingRegistry().models[alias]['upstream_model'], 'minimax/MiniMax-H3')
    def test_aliases_routes_discovery_and_defaults(self):
        for alias in ('minimax-h3', 'MiniMax-H3'):
            self.assertEqual(provider_for_model(alias), 'minimax')
            self.assertEqual(route_for(alias, 2, REVISION), 'minimax_h3_v2')
            for schema, revision in [(1, None), (2, None), (2, 'unknown')]:
                with self.assertRaises(ProviderAdapterError): route_for(alias, schema, revision)
            self.assertIsInstance(adapter_for_job(job(status='completed')), MiniMaxAdapter)
            contract = video_contract(alias)
            self.assertEqual([op['id'] for op in contract['operations']], PROFILES)
            self.assertEqual(contract['audio_mode'], 'provider_managed')
            self.assertNotIn('generateAudio', contract['operations'][0]['settings'])
        self.assertEqual(validate(request()), {'resolution': '768p', 'duration': 5, 'aspectRatio': '16:9', 'outputCount': 1})
        for path in ('/v1/chat/completions', '/v1/responses', '/v1/images/generations', '/v1/videos'):
            self.assertEqual(apply_request_policy(path, {'model': 'MiniMax-H3'})[1].code, 'MINIMAX_REQUIRES_DURABLE_JOB')

    def test_valid_boundaries_and_audio_only_references(self):
        for duration in (4, 15):
            validate_video(request(settings={'duration': duration, 'resolution': '2k'}))
        validate_video(request('generate.multimodal_references', media=[media('audio', 'reference_audio')]))
        refs = [media(index=i) for i in range(9)] + [media('video', 'reference_video', i) for i in range(3)]
        validate_video(request('generate.multimodal_references', prompt='x'*7000, media=refs))

    def test_invalid_combinations_and_settings(self):
        bad = [request(prompt=' '), request(prompt='x'*7001), request(operation='edit'),
            request(voice_ids=['voice']), request(previous_job_id='gen_previous'),
            request(media=[media()]), request('generate.first_frame', media=[media(role='last_frame')]),
            request('generate.first_last_frames', media=[media(role='first_frame')]),
            request('generate.references', media=[media('audio', 'reference_audio')]),
            request('generate.multimodal_references', media=[media(role='first_frame'), media()]),
            request('generate.multimodal_references', media=[media('video', 'source')]),
            request('generate.references', media=[media(index=1)]),
            request('generate.references', media=[media(index=i) for i in range(10)]),
            request('generate.multimodal_references', media=[media(index=i) for i in range(9)]+[media('video','reference_video',i) for i in range(3)]+[media('audio','reference_audio')])]
        for settings in ({'duration': 3}, {'duration': 16}, {'duration': 4.0}, {'duration': True},
                         {'resolution': '720p'}, {'generateAudio': True}, {'generate_audio': False},
                         {'seed': 1}, {'outputCount': 2}, {'aspectRatio': 'adaptive'},
                         {'aspectRatio': '16:9', 'aspect_ratio': '9:16'}):
            bad.append(request(settings=settings))
        for r in bad:
            with self.subTest(profile=r.profile_id, settings=r.settings), self.assertRaises(ValueError): validate_video(r)

    def test_semantic_changes_affect_hash(self):
        a = request(); b = request(settings={'resolution': '2k'})
        self.assertNotEqual(_hash_request_v2(a, {}), _hash_request_v2(b, {}))


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_inline_body_cap_and_mov_transport_limit(self):
        r = request('generate.multimodal_references', media=[media('video', 'reference_video', upload_field='video')])
        with patch.dict(os.environ, {'MINIMAX_API_KEY': 'fixture'}), \
             patch('minimax_video_adapter.inspect_media', return_value=('video/quicktime', 2)), \
             patch('minimax_video_adapter._json_request', AsyncMock()) as post:
            with self.assertRaisesRegex(ProviderAdapterError, 'HTTPS URL for MOV'):
                await MiniMaxAdapter().submit(r, job_id='gen_h3', callback_url=None,
                    upload_bytes={'video': ('clip.mov', b'mov', 'video/quicktime')})
            post.assert_not_awaited()
        r = request('generate.references', media=[media(index=i, upload_field=f'image{i}') for i in range(2)])
        with patch.dict(os.environ, {'MINIMAX_API_KEY': 'fixture'}), \
             patch('minimax_video_adapter.inspect_media', return_value=('image/png', 0)), \
             patch('minimax_video_adapter._json_request', AsyncMock()) as post:
            with self.assertRaisesRegex(ProviderAdapterError, '64 MB'):
                await MiniMaxAdapter().submit(r, job_id='gen_h3', callback_url=None,
                    upload_bytes={f'image{i}': ('frame.png', b'x' * 24_000_000, 'image/png') for i in range(2)})
            post.assert_not_awaited()

    async def test_multipart_native_video_route_is_rejected(self):
        from gateway_request_policy import GatewayRequestPolicyMiddleware
        downstream = AsyncMock()
        boundary = 'test-h3-boundary'
        raw = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\nminimax-h3\r\n--{boundary}--\r\n').encode()
        receive = AsyncMock(return_value={'type': 'http.request', 'body': raw, 'more_body': False})
        send = AsyncMock()
        await GatewayRequestPolicyMiddleware(downstream)({'type': 'http', 'method': 'POST', 'path': '/v1/videos',
            'headers': [(b'content-type', f'multipart/form-data; boundary={boundary}'.encode())]}, receive, send)
        downstream.assert_not_awaited()
        self.assertEqual(send.await_args_list[0].args[0]['status'], 400)
    async def test_every_profile_exact_mapping(self):
        cases = [[], [media(role='first_frame')], [media(role='last_frame')],
            [media(role='first_frame'), media(role='last_frame')], [media(), media(index=1)],
            [media(), media('video', 'reference_video'), media('audio', 'reference_audio')]]
        for profile, inputs in zip(PROFILES, cases):
            with self.subTest(profile=profile), patch.dict(os.environ, {'MINIMAX_API_KEY': 'fixture'}), \
                 patch('minimax_video_adapter._download_media', AsyncMock(return_value=('input.png', b'bytes', 'image/png'))), \
                 patch('minimax_video_adapter.inspect_media', return_value=('image/png', 2)), \
                 patch('minimax_video_adapter._json_request', AsyncMock(return_value={'task_id': 'task-h3'})) as post:
                result = await MiniMaxAdapter().submit(request(profile, media=inputs), job_id='gen_h3', callback_url=None)
            self.assertEqual(result.provider_request_id, 'task-h3')
            self.assertEqual(post.await_count, 1)
            self.assertEqual(post.await_args.args, ('POST', 'https://api.minimax.io/v2/video_generation'))
            b = post.await_args.kwargs['body']
            self.assertEqual((b['model'], b['resolution'], b['duration']), ('MiniMax-H3', '768P', 5))
            self.assertEqual(b['ratio'], '16:9' if not inputs else 'adaptive')
            self.assertEqual(b['content'][0], {'type': 'text', 'text': 'Waves and surf sounds'})
            self.assertEqual([x['role'] for x in b['content'][1:]], ['reference_image' if m['role']=='reference' else m['role'] for m in inputs])
            self.assertEqual([x[x['type']]['url'] for x in b['content'][1:]], [m['url'] for m in inputs])
            self.assertEqual(post.await_args.kwargs['headers']['Authorization'], 'Bearer fixture')
            self.assertNotIn('callback_url', b)
            self.assertNotIn('generate_audio', b)

    async def test_upload_encoding_and_missing_key(self):
        r = request('generate.first_frame', media=[media(role='first_frame', upload_field='frame')])
        with patch.dict(os.environ, {'MINIMAX_API_KEY': 'fixture'}), \
             patch('minimax_video_adapter.inspect_media', return_value=('image/png', 0)), \
             patch('minimax_video_adapter._json_request', AsyncMock(return_value={'task_id': 'task'})) as post:
            await MiniMaxAdapter().submit(r, job_id='gen_h3', callback_url=None, upload_bytes={'frame': ('x.png',b'image','image/png')})
        self.assertEqual(post.await_args.kwargs['body']['content'][1]['image_url']['url'], 'data:image/png;base64,'+base64.b64encode(b'image').decode())
        with patch.dict(os.environ, {'MINIMAX_API_KEY': ''}), patch('minimax_video_adapter._json_request', AsyncMock()) as post:
            with self.assertRaises(ProviderAdapterError) as caught:
                await MiniMaxAdapter().submit(request(), job_id='gen_h3', callback_url=None)
            self.assertEqual(caught.exception.code, 'PROVIDER_NOT_CONFIGURED'); post.assert_not_awaited()

    async def test_preflight_limits_prevent_submission(self):
        r = request('generate.multimodal_references', media=[media('video','reference_video',i) for i in range(2)])
        with patch.dict(os.environ, {'MINIMAX_API_KEY': 'fixture'}), \
             patch('minimax_video_adapter._download_media', AsyncMock(return_value=('a.mp4',b'video','video/mp4'))), \
             patch('minimax_video_adapter.inspect_media', return_value=('video/mp4', 8)), \
             patch('minimax_video_adapter._json_request', AsyncMock()) as post:
            with self.assertRaisesRegex(ProviderAdapterError, 'total reference video'):
                await MiniMaxAdapter().submit(r, job_id='gen_h3', callback_url=None)
            post.assert_not_awaited()

    async def test_http_failures_and_unknown_submission_never_resubmit(self):
        for method in ('POST','GET'):
            for status in (400,401,402,422,429,500,529):
                response = httpx.Response(status, json={'error': {'message': 'fixture error'}, 'usage': {'output_seconds': 2}}, request=httpx.Request(method,'https://api.minimax.io'))
                with self.subTest(method=method,status=status), patch.dict(os.environ, {'MINIMAX_API_KEY': 'fixture'}), \
                     patch('generation_job_adapters.httpx.AsyncClient') as client:
                    client.return_value.__aenter__.return_value.request=AsyncMock(return_value=response)
                    with self.assertRaises(ProviderAdapterError) as caught:
                        await MiniMaxAdapter()._request(method, '/video_generation')
                    self.assertEqual(caught.exception.outcome_unknown, method=='POST' and status>=500)
                    self.assertEqual(caught.exception.retryable, status==429 or method=='GET' and status>=500)
                    self.assertEqual(caught.exception.usage, {'output_seconds': 2})
                    client.return_value.__aenter__.return_value.request.assert_awaited_once()
        with patch.dict(os.environ, {'MINIMAX_API_KEY': 'fixture'}), patch('generation_job_adapters.httpx.AsyncClient') as client:
            client.return_value.__aenter__.return_value.request=AsyncMock(side_effect=httpx.ReadTimeout('timeout'))
            with self.assertRaises(ProviderAdapterError) as caught: await MiniMaxAdapter()._request('POST','/video_generation')
            self.assertTrue(caught.exception.outcome_unknown)
            client.return_value.__aenter__.return_value.request.assert_awaited_once()
        with patch.object(MiniMaxAdapter,'_headers', return_value={}), patch.object(MiniMaxAdapter,'_request', AsyncMock(return_value={})) as post:
            with self.assertRaises(ProviderAdapterError) as caught: await MiniMaxAdapter().submit(request(),job_id='gen_h3',callback_url=None)
            self.assertTrue(caught.exception.outcome_unknown); post.assert_awaited_once()

    async def test_all_states_usage_and_invalid_asset(self):
        for provider, public in [('queued','queued'),('running','in_progress'),('succeeded','completed'),('failed','failed'),('cancelled','cancelled')]:
            data=receipt(provider)
            if provider=='failed': data['task']['error']={'code':'1026','message':'Rejected'}
            with patch.object(MiniMaxAdapter,'_request',AsyncMock(return_value=data)) as get:
                result=await MiniMaxAdapter().retrieve(job(status='in_progress'))
            self.assertEqual(result.status,public);self.assertEqual(result.usage,data['task']['usage'])
            self.assertEqual(result.served_model,'MiniMax-H3')
            self.assertEqual(get.await_args.args,('GET','/query/video_generation/task-h3'))
            if provider=='succeeded':
                self.assertEqual(result.result_metadata['outputs'][0]['mime_type'],'video/mp4')
                self.assertEqual(result.result_metadata['actual_duration_seconds'],4)
        data=receipt();data['task']['content']={};data['task'].pop('resolution')
        with patch.object(MiniMaxAdapter,'_request',AsyncMock(return_value=data)):
            result=await MiniMaxAdapter().retrieve(job(status='in_progress'))
        self.assertEqual(result.error_code,'PROVIDER_RESULT_INVALID')
        self.assertEqual(result.usage['output_seconds'],4)
        self.assertIsNone(result.result_metadata['resolution'])


class MediaTests(unittest.TestCase):
    def test_media_metadata_boundaries(self):
        def probe(kind, **changes):
            stream={'codec_type':'video','codec_name':'h264','width':256,'height':256,'avg_frame_rate':'24/1'}
            fmt={'format_name':'mov,mp4','duration':'2'}
            if kind=='audio':stream={'codec_type':'audio','codec_name':'mp3'};fmt['format_name']='mp3'
            stream.update(changes)
            return {'streams':[stream],'format':fmt}
        cases=[('video',{},True),('video',{'width':255},False),('video',{'height':5761},False),
            ('video',{'width':1024,'height':256},False),('video',{'avg_frame_rate':'24000/1001'},True),
            ('video',{'avg_frame_rate':'22/1'},False),('video',{'codec_name':'vp9'},False),('audio',{},True)]
        for kind,change,valid in cases:
            with self.subTest(kind=kind,change=change), patch('minimax_video_adapter.probe_media_bytes',return_value=probe(kind,**change)):
                if valid: self.assertEqual(inspect_media(kind,b'media','file')[1],2)
                else:
                    with self.assertRaises(ProviderAdapterError): inspect_media(kind,b'media','file')
        for duration in ('1.99','15.01','nan'):
            p=probe('audio');p['format']['duration']=duration
            with patch('minimax_video_adapter.probe_media_bytes',return_value=p), self.assertRaises(ProviderAdapterError):inspect_media('audio',b'a','a.mp3')
        with self.assertRaises(ProviderAdapterError):inspect_media('audio',b'x'*(15*1024*1024+1),'a.mp3')

    def test_real_ffprobe_mp4_and_png(self):
        # Synthetic local fixture; no model generation and no network.
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'clip.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=256x256:r=24','-t','2','-c:v','libx264','-pix_fmt','yuv420p',str(path)],check=True,capture_output=True)
            self.assertEqual(inspect_media('video',path.read_bytes(),path.name),('video/mp4',2))
            def chunk(name,value):return struct.pack('>I',len(value))+name+value+struct.pack('>I',zlib.crc32(name+value)&0xffffffff)
            png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',256,256,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\x00'+b'\x00\x00\xff'*256)*256))+chunk(b'IEND',b'')
            self.assertEqual(inspect_media('image',png,'frame.png'),('image/png',0))


class PricingTests(unittest.TestCase):
    def test_metered_output_input_allowance_and_no_double_token_charge(self):
        prices=PricingRegistry()
        for resolution,rate in [('768p',Decimal('.08')),('2k',Decimal('.13'))]:
            for images in (0,5,6,9):
                r=request(settings={'resolution':resolution})
                profile=prices.select(r.model,'generation_job',options_for(r.model_dump()))
                usage=extract('minimax_h3',{'output_seconds':4,'input_seconds':3,'input_image_count':images,'input_audio_seconds':15,'total_tokens':999999})
                result=prices.calculate(profile,usage,served_model='MiniMax-H3',served_options={'resolution':resolution})
                self.assertEqual(Decimal(result['cost_usd']),7*rate+max(images-5,0)*Decimal('.04'))
                self.assertEqual(result['cost_source'],'posted_api_rate')
                self.assertIsNone(result['reported_cost_usd'])
        for missing in ('output_seconds','input_seconds','input_image_count'):
            raw={'output_seconds':4,'input_seconds':0,'input_image_count':0};raw.pop(missing)
            with self.assertRaises(PricingError):extract('minimax_h3',raw)
        profile=prices.select('MiniMax-H3','generation_job')
        for options in ({}, {'resolution':'2k'}, {'resolution':None}):
            with self.assertRaises(PricingError): prices.calculate(profile,{'output_seconds':'4','input_seconds':'0','extra_input_images':'0'},served_options=options)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_rejected_before_job_and_replay_never_submits(self):
        user=UserAPIKeyAuth(api_key='test')
        with patch('generation_job_routes._parse_request',AsyncMock(return_value=(request(prompt=''),{}))), \
             patch('generation_job_routes.repository.create_or_get',AsyncMock()) as create:
            with self.assertRaises(HTTPException) as caught:await create_generation_job(http_request(),Response(),'key',user)
            self.assertEqual(caught.exception.status_code,422);create.assert_not_awaited()
        existing=job(status='in_progress')
        with patch('generation_job_routes._parse_request',AsyncMock(return_value=(request(),{}))), \
             patch('generation_job_routes.repository.create_or_get',AsyncMock(return_value=(existing,False,False))) as create, \
             patch.object(MiniMaxAdapter,'submit',AsyncMock()) as submit:
            result=await create_generation_job(http_request(),Response(),'key',user)
            self.assertEqual(result.id,existing['id']);submit.assert_not_awaited()
            self.assertEqual(create.await_args.kwargs['adapter_revision'],'minimax_h3_v2@2026-10-02')
        with patch('generation_job_routes._parse_request',AsyncMock(return_value=(request(),{}))), \
             patch('generation_job_routes.repository.create_or_get',AsyncMock(return_value=(existing,False,True))):
            with self.assertRaises(HTTPException) as caught:await create_generation_job(http_request(),Response(),'key',user)
            self.assertEqual(caught.exception.status_code,409)

    async def test_poll_records_served_metadata_and_usage_before_job_write(self):
        data=receipt();data['task']['resolution']='2K'
        with patch.object(MiniMaxAdapter,'_request',AsyncMock(return_value=data)):
            status=await MiniMaxAdapter().retrieve(job(status='in_progress'))
        events=[]
        async def record(j):events.append(('receipt',j))
        async def apply(*args):events.append(('job',None));return job(status='completed',usage=status.usage)
        with patch('generation_job_routes._verify_internal',AsyncMock()), \
             patch('generation_job_routes.repository.get',AsyncMock(return_value=job(status='in_progress'))), \
             patch.object(MiniMaxAdapter,'retrieve',AsyncMock(return_value=status)), \
             patch('generation_job_routes._record_spend',side_effect=record), \
             patch('generation_job_routes.repository.apply_provider_status',side_effect=apply):
            await poll_generation_job('gen_h3',http_request())
        self.assertEqual(events[0][0],'receipt')
        self.assertEqual(events[0][1]['request_metadata']['resolution'],'2k')
        self.assertEqual(events[0][1]['usage']['output_seconds'],4)

    async def test_deadline_final_poll_and_transient_retry(self):
        for expired in (False,True):
            j=job(status='in_progress');j['deadline_at']=datetime.now(timezone.utc)+timedelta(seconds=-1 if expired else 600)
            with patch('generation_job_routes._verify_internal',AsyncMock()), \
                 patch('generation_job_routes.repository.get',AsyncMock(return_value=j)), \
                 patch.object(MiniMaxAdapter,'retrieve',AsyncMock(side_effect=ProviderAdapterError('busy',retryable=True))), \
                 patch('generation_job_routes.repository.mark_expired',AsyncMock(return_value={**j,'status':'expired'})) as expire, \
                 patch('generation_job_routes.repository.record_poll_error',AsyncMock(return_value=j)) as retry, \
                 patch('generation_job_routes.enqueue_poll',AsyncMock()),patch('generation_job_routes._record_spend',AsyncMock()):
                await poll_generation_job('gen_h3',http_request())
                self.assertEqual(expire.await_count,int(expired));self.assertEqual(retry.await_count,int(not expired))
                if not expired:self.assertGreaterEqual((retry.await_args.kwargs['next_poll_at']-datetime.now(timezone.utc)).total_seconds(),9)
        now=datetime.now(timezone.utc)
        with patch('generation_job_scheduler.random.uniform',return_value=.8):self.assertGreaterEqual((next_poll_time(0,now=now,minimum_delay=10)-now).total_seconds(),10)

    async def test_content_owner_scope_and_expired_url_refresh(self):
        user=UserAPIKeyAuth(api_key='test');j=job(status='completed',result_url='https://cdn.example/old.mp4')
        async def chunks():yield b'original audiovisual bytes'
        with patch.object(MiniMaxAdapter,'_request',AsyncMock(return_value=receipt())):
            renewed=await MiniMaxAdapter().retrieve(j)
        for output_route in (False,True):
            j['request_metadata']['outputs']=[{'url':j['result_url'],'mime_type':'video/mp4','role':'video'}]
            with patch('generation_job_routes.repository.get',AsyncMock(return_value=j)) as get, \
                 patch('generation_job_routes._remote_content',AsyncMock(side_effect=[ProviderAdapterError('expired',code='CONTENT_URL_EXPIRED'),(chunks(),200,'video/mp4',{})])) as remote, \
                 patch.object(MiniMaxAdapter,'retrieve',AsyncMock(return_value=renewed)) as refresh, \
                 patch('generation_job_routes.repository.refresh_result_url',AsyncMock(return_value=j)), \
                 patch('generation_job_routes.repository.pool',AsyncMock()) as pool:
                response=await (get_generation_job_output('gen_h3',0,http_request(),user) if output_route else get_generation_job_content('gen_h3',http_request(),user))
                self.assertEqual(b''.join([part async for part in response.body_iterator]),b'original audiovisual bytes')
                self.assertEqual(len(get.await_args.args),2)
                refresh.assert_awaited_once();self.assertEqual(remote.await_args.args[0],renewed.result_url)
        with patch('generation_job_routes.repository.get',AsyncMock(return_value=None)),patch.object(MiniMaxAdapter,'content',AsyncMock()) as content:
            with self.assertRaises(HTTPException) as caught:await get_generation_job_content('gen_h3',http_request(),user)
            self.assertEqual(caught.exception.status_code,404);content.assert_not_awaited()

import base64
import os
import unittest
from unittest.mock import AsyncMock, patch

from generation_job_models import GenerationJobCreateV2, v2_to_v1
from generation_job_adapters import ProviderAdapterError, route_for
from expanded_video_adapters import ExpandedSeedance, ExpandedGrok, ExpandedVeo
from video_capabilities import REVISION, profiles, validate, settings_for


def media(kind='image', role='reference', index=0, **kw):
    return dict(slot_id=role,index=index,kind=kind,role=role,url=f'https://example.test/{kind}-{index}',**kw)


def request(model='seedance-2.5',profile='generate.text',**kw):
    return GenerationJobCreateV2(request_schema_version=2, model=model, contract_revision=REVISION,
        profile_id=profile, operation=profile.split('.')[0], **kw)


class Contracts(unittest.TestCase):
    def test_routes_preserve_old_adapter_selection(self):
        self.assertEqual(route_for('seedance-2.0',2),'byteplus_ark_v3')
        self.assertEqual(route_for('seedance-2.0',2,REVISION),'byteplus_expanded_v2')
        self.assertEqual(route_for('veo-3.1-fast',2,REVISION),'vertex_veo_expanded_v2')

    def test_exact_model_limits_not_family_wide(self):
        self.assertNotIn('generate.references',profiles('veo-3.1-lite'))
        with self.assertRaises(ValueError): validate(request('veo-3.1-lite','generate.references',media=[media()]))
        with self.assertRaises(ValueError): validate(request('seedance-2.0-fast',settings={'resolution':'1080p'}))
        validate(request('seedance-2.0',settings={'resolution':'4k','duration':-1}))

    def test_reference_order_is_never_silently_sorted(self):
        with self.assertRaises(ValueError): validate(request(profile='generate.references',media=[media(index=1),media(index=0)]))
        validate(request(profile='generate.references',media=[media(),media(index=1)]))

    def test_seedance_audio_only_is_exact25(self):
        validate(request(profile='generate.multimodal_references',media=[media('audio','reference_audio')]))
        with self.assertRaises(ValueError):validate(request('seedance-2.0',profile='generate.multimodal_references',media=[media('audio','reference_audio')]))

    def test_seedance_draft_completion_omits_inherited_fields(self):
        r=request(profile='generate.from_draft',previous_job_id='gen_draft',settings={'resolution':'1080p','return_last_frame':True})
        validate(r)
        for settings in ({'duration':4},{'generateAudio':False},{'resolution':'720p'}):
            with self.subTest(settings=settings),self.assertRaises(ValueError):validate(r.model_copy(update={'settings':settings}))
        with self.assertRaises(ValueError): validate(r.model_copy(update={'prompt':'a different prompt'}))

    def test_draft_resolution_and_edit_duration_are_provider_constraints(self):
        validate(request(profile='generate.draft',settings={'resolution':'480p'}))
        with self.assertRaises(ValueError):validate(request(profile='generate.draft',settings={'resolution':'720p'}))
        with self.assertRaises(ValueError):validate(request(profile='edit.source_video',media=[media('video','source')],settings={'duration':4}))

    def test_grok_keyframe_grid_and_positions(self):
        validate(request('grok-video-1.5','generate.keyframes',media=[media(role='keyframe',timestamp_seconds=1/3)],settings={'duration':2}))
        for when in (.4,2):
            with self.subTest(when=when),self.assertRaises(ValueError):validate(request('grok-video-1.5','generate.keyframes',media=[media(role='keyframe',timestamp_seconds=when)],settings={'duration':2}))

    def test_audio_omission_is_distinct_from_explicit_false(self):
        omitted=v2_to_v1(request('gemini-omni-1.1-flash'))
        self.assertNotIn('generate_audio',omitted.model_fields_set)
        explicit=v2_to_v1(request('grok-video-1.5',settings={'generateAudio':False}))
        self.assertIn('generate_audio',explicit.model_fields_set)
        self.assertFalse(explicit.generate_audio)

    def test_video_upload_not_silently_sent_as_unsupported_base64(self):
        m=media('video','source');m.pop('url');m['upload_field']='source'
        with self.assertRaisesRegex(ValueError,'HTTPS'):validate(request(profile='edit.source_video',media=[m]))

    def test_omni_declares_provider_managed_audio_without_fake_toggle(self):
        self.assertNotIn('generateAudio',settings_for('gemini-omni-1.1-flash','generate.text'))
        with self.assertRaises(ValueError):validate(request('gemini-omni-1.1-flash',settings={'generateAudio':False}))


class Wire(unittest.IsolatedAsyncioTestCase):
    async def seedance(self,r):
        with patch.dict(os.environ,{'BYTEDANCE_API_KEY':'fixture'}),patch('expanded_video_adapters._json_request',AsyncMock(return_value={'id':'task-fixture'})) as post:
            result=await ExpandedSeedance().submit(r,job_id='gen_test',callback_url=None)
        return post.await_args.kwargs['body'],result

    async def test_seedance_reference_order_kinds_and_automatic_duration(self):
        r=request(profile='generate.multimodal_references',media=[media(),media(index=1),media('video','reference_video'),media('audio','reference_audio')],settings={'duration':-1,'generateAudio':False})
        body,result=await self.seedance(r)
        self.assertEqual([c['type'] for c in body['content']],['image_url','image_url','video_url','audio_url'])
        self.assertEqual(body['duration'],-1);self.assertIs(body['generate_audio'],False)
        self.assertTrue(result.request_metadata['has_input_video'])
        self.assertEqual(body['model'],'dreamina-seedance-2-5-260628')

    async def test_seedance_edit_defaults_and_return_last_frame(self):
        body,_=await self.seedance(request(profile='edit.source_video',media=[media('video','source')],settings={'return_last_frame':True,'output_format':'mov'}))
        self.assertEqual((body['duration'],body['ratio'],body['omni_reference_task_type']),(-1,'adaptive','edit'))
        self.assertTrue(body['return_last_frame']);self.assertEqual(body['output_format'],'mov')

    async def test_draft_final_sends_exact_owned_task_and_only_new_controls(self):
        r=request(profile='generate.from_draft',previous_job_id='gen_owned',settings={'return_last_frame':True});r._previous_provider_id='cgt-exact';r._previous_metadata={'has_input_video':True}
        body,result=await self.seedance(r)
        self.assertEqual(body['content'],[{'type':'draft_task','draft_task':{'id':'cgt-exact'}}])
        self.assertEqual(body['resolution'],'1080p');self.assertNotIn('duration',body);self.assertNotIn('ratio',body)
        self.assertTrue(result.request_metadata['has_input_video'])

    async def test_seedance_preserves_video_and_last_frame_results(self):
        with patch('expanded_video_adapters._json_request',AsyncMock(return_value={'status':'succeeded','content':{'video_url':'https://example.test/out.mov','last_frame_url':'https://example.test/end.jpg'},'output_format':'mov','usage':{'completion_tokens':123},'model':'dreamina-seedance-2-5-260628'})),patch.dict(os.environ,{'BYTEDANCE_API_KEY':'fixture'}):
            s=await ExpandedSeedance().retrieve({'model':'seedance-2.5','provider_request_id':'cgt-test'})
        self.assertEqual(len(s.result_metadata['outputs']),2);self.assertEqual(s.result_mime_type,'video/quicktime');self.assertEqual(s.usage['completion_tokens'],123)

    async def test_grok_last_frame_keyframes_and_false_audio(self):
        r=request('grok-video-1.5','generate.keyframes',settings={'duration':2,'generateAudio':False},media=[media(role='keyframe',timestamp_seconds=1),media()])
        with patch.dict(os.environ,{'GROK_API_KEY':'fixture'}),patch('expanded_video_adapters._json_request',AsyncMock(return_value={'request_id':'grok-id'})) as post:
            await ExpandedGrok().submit(r,job_id='gen_x',callback_url=None)
        body=post.await_args.kwargs['body'];self.assertEqual(body['keyframes'][0]['timestamp_s'],1);self.assertIs(body['generate_audio'],False);self.assertEqual(len(body['reference_images']),1)

    def test_veo_regional_endpoint_ignores_unrelated_global_gemini_setting(self):
        with patch.dict(os.environ,{'GOOGLE_CLOUD_PROJECT':'fixture','GOOGLE_CLOUD_LOCATION':'global'}):
            endpoint=ExpandedVeo()._model_url('veo-3.1-lite-generate-001','predictLongRunning')
        self.assertEqual(endpoint,'https://us-central1-aiplatform.googleapis.com/v1/projects/fixture/locations/us-central1/publishers/google/models/veo-3.1-lite-generate-001:predictLongRunning')

    async def test_veo_first_last_inline_no_bucket_required(self):
        r=request('veo-3.1-lite','generate.first_last_frames',media=[media(role='first_frame'),media(role='last_frame')],settings={'duration':4,'resolution':'720p','generateAudio':False})
        with patch.dict(os.environ,{'GOOGLE_CLOUD_PROJECT':'fixture'}),patch('expanded_video_adapters._json_request',AsyncMock(return_value={'name':'projects/exact/operations/test'})) as post,patch('expanded_video_adapters.VertexAdapter._vertex_headers',AsyncMock(return_value={})),patch.object(ExpandedVeo,'_inline_image',AsyncMock(side_effect=[{'bytesBase64Encoded':'first','mimeType':'image/png'},{'bytesBase64Encoded':'last','mimeType':'image/png'}])):
            result=await ExpandedVeo().submit(r,job_id='gen_test',callback_url=None)
        b=post.await_args.kwargs['body'];self.assertEqual(b['instances'][0]['lastFrame']['bytesBase64Encoded'],'last');self.assertFalse(b['parameters']['generateAudio']);self.assertEqual(result.provider_request_id,'projects/exact/operations/test')

    async def test_veo_inline_result_retrievable_and_measured(self):
        adapter=ExpandedVeo();data={'done':True,'response':{'videos':[{'bytesBase64Encoded':base64.b64encode(b'original-mp4').decode()}]}}
        job={'model':'veo-3.1-lite','provider_request_id':'operation-id','request_metadata':{'upstream_model':'veo-3.1-lite-generate-001'}}
        with patch.object(adapter,'_fetch',AsyncMock(return_value=data)),patch('expanded_video_adapters.probe_media_bytes',return_value={'format':{'duration':'4.125'}}):
            result=await adapter.retrieve(job);content=await adapter.content({**job,'result_url':result.result_url})
        self.assertEqual(content.content,b'original-mp4');self.assertEqual(result.usage['output_video_seconds'],'4.125')

    async def test_veo_multiple_outputs_and_extension_bills_only_new_seconds(self):
        adapter=ExpandedVeo();data={'done':True,'response':{'videos':[{'bytesBase64Encoded':base64.b64encode(b'first').decode()},{'bytesBase64Encoded':base64.b64encode(b'second').decode()}]}}
        job={'model':'veo-3.1','provider_request_id':'op','request_metadata':{'upstream_model':'veo-3.1-generate-001','operation':'extend'}}
        validate(request('veo-3.1','extend.source_video',media=[media('video','source')],settings={'outputCount':2}))
        with patch.object(adapter,'_fetch',AsyncMock(return_value=data)),patch('expanded_video_adapters.probe_media_bytes',return_value={'format':{'duration':'37'}}):
            result=await adapter.retrieve(job)
            second=await adapter.content({**job,'result_url':result.result_metadata['outputs'][1]['url']})
        self.assertEqual(result.usage['output_video_seconds'],'14')
        self.assertEqual(result.result_metadata['outputs'][0]['duration_seconds'],'37')
        self.assertEqual(second.content,b'second')

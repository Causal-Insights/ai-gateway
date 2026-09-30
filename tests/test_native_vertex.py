import json,os,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from uuid import uuid4
import httpx
from fastapi import FastAPI,HTTPException
import native_vertex as native
from generation_job_repository import GenerationJobRepository

class Payload(unittest.IsolatedAsyncioTestCase):
 async def test_text_exact_identity_and_no_arbitrary_tools(self):
  value=await native.payload({'model':'gemini-omni-1.1-flash','input':'hello'},('app','a'))
  self.assertEqual(value['model'],'gemini-omni-1.1-flash-preview')
  self.assertEqual(value['response_format'],{'type':'text','mime_type':'text/plain'});self.assertIs(value['stream'],False)
  with self.assertRaises(HTTPException):await native.payload({'model':'gemini-omni-1.1-flash','input':'hi','tools':[{'type':'search'}]},('app','a'))
 async def test_ordered_media_and_owned_continuation(self):
  with patch.object(native.VertexAdapter,'_omni_media_content',AsyncMock(side_effect=[({'type':'image','data':'one','mime_type':'image/png'},b'1'),({'type':'video','data':'two','mime_type':'video/mp4'},b'2')])),patch.object(native,'owned',AsyncMock(return_value={'model':'gemini-omni-1.1-flash','provider_id':'upstream-exact'})):
   value=await native.payload({'model':'gemini-omni-1.1-flash','previous_interaction_id':'interaction_owned','input':[{'type':'image','uri':'https://fixture.test/1'},{'type':'text','text':'Between'},{'type':'video','uri':'https://fixture.test/2'}]},('app','alice'))
  self.assertEqual([x['type'] for x in value['input']],['image','text','video'])
  self.assertEqual(value['previous_interaction_id'],'upstream-exact')

@unittest.skipUnless(os.environ.get('RUN_VOICE_DB_TESTS')=='1','requires disposable PostgreSQL')
class Lifecycle(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.repo=GenerationJobRepository()
  async def migrate():await self.repo._pool.execute((Path(__file__).parents[1]/'migrations/008_vertex_interactions.sql').read_text())
  self.repo._migrate=migrate
  self.account=SimpleNamespace(check_budgets=AsyncMock(),begin=AsyncMock(side_effect=lambda **kw:kw['accounting_id']),attempt=AsyncMock(),observe=AsyncMock(),finish=AsyncMock())
  self.upstream=AsyncMock(return_value={'id':'provider-exact','model':'gemini-omni-1.1-flash-preview','status':'completed','steps':[{'type':'model_output','content':[{'type':'text','text':'Hello'}]}],'usage':{'total_input_tokens':3,'total_output_tokens':2,'total_thought_tokens':0,'output_tokens_by_modality':[{'modality':'text','tokens':2}]}})
  self.patches=[patch.object(native,'repository',self.repo),patch.object(native,'accounting',self.account),patch.object(native,'_json_request',self.upstream),patch.object(native,'permitted',AsyncMock()),patch.object(native,'check_context'),patch.object(native,'finalize_reservation',AsyncMock()),patch.object(native.VertexAdapter,'_vertex_headers',AsyncMock(return_value={})),patch.dict(os.environ,{'GOOGLE_CLOUD_PROJECT':'fixture'})]
  for p in self.patches:p.start()
  self.app_id=uuid4().hex;self.user=SimpleNamespace(metadata={'gateway_app_id':self.app_id,'gateway_resource_delegate':True},api_key='fixture-key')
  app=FastAPI();app.include_router(native.router);app.dependency_overrides[native.user_api_key_auth]=lambda:self.user
  self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://gateway',headers={'X-Gateway-Resource-Owner':'alice','Idempotency-Key':uuid4().hex})
 async def asyncTearDown(self):
  await self.client.aclose();await (await self.repo.pool()).execute('delete from gateway_vertex_interactions where app_id=$1',self.app_id);await self.repo.close()
  for p in reversed(self.patches):p.stop()
 async def test_replay_rotation_and_owner_denial(self):
  body={'model':'gemini-omni-1.1-flash','input':'hello'}
  first=await self.client.post('/v1/interactions',json=body);self.assertEqual(first.status_code,200,first.text)
  self.user.api_key='rotated';second=await self.client.post('/v1/interactions',json=body)
  self.assertEqual(second.json()['id'],first.json()['id']);self.assertEqual(self.upstream.await_count,1);self.account.finish.assert_awaited_once()
  foreign=await self.client.get('/v1/interactions/'+first.json()['id'],headers={'X-Gateway-Resource-Owner':'bob'});self.assertEqual(foreign.status_code,404)
  raw=await self.client.get('/v1/interactions/provider-exact');self.assertEqual(raw.status_code,404)
 async def test_ambiguous_create_never_reposts(self):
  self.upstream.side_effect=httpx.ReadTimeout('unknown')
  with self.assertRaises(httpx.ReadTimeout):await self.client.post('/v1/interactions',json={'model':'gemini-omni-1.1-flash','input':'hello'})
  replay=await self.client.post('/v1/interactions',json={'model':'gemini-omni-1.1-flash','input':'hello'})
  self.assertEqual(replay.status_code,202);self.assertEqual(replay.json()['status'],'outcome_unknown');self.assertEqual(self.upstream.await_count,1)
 async def test_background_recovers_once_and_conflicting_id_fails(self):
  complete=self.upstream.return_value;self.upstream.return_value={'id':'provider-exact','status':'in_progress'}
  first=await self.client.post('/v1/interactions',json={'model':'gemini-omni-1.1-flash','input':'hello','background':True});self.upstream.return_value=complete
  url='/v1/interactions/'+first.json()['id'];a=await self.client.get(url);b=await self.client.get(url)
  self.assertEqual(a.json()['status'],'completed');self.assertEqual(b.json()['steps'],complete['steps']);self.account.finish.assert_awaited_once()
  conflict=await self.client.post('/v1/interactions',json={'model':'gemini-omni-1.1-flash','input':'different'})
  self.assertEqual(conflict.status_code,409)

 async def test_definitive_rejection_retains_handle_and_returns_provider_error(self):
  self.upstream.side_effect=native.ProviderAdapterError('Invalid argument',code='PROVIDER_REJECTED_SUBMISSION',status_code=400)
  response=await self.client.post('/v1/interactions',json={'model':'gemini-omni-1.1-flash','input':'hello'})
  self.assertEqual(response.status_code,400);self.assertTrue(response.json()['accounting_id']);self.assertEqual(response.json()['status'],'failed')
  replay=await self.client.post('/v1/interactions',json={'model':'gemini-omni-1.1-flash','input':'hello'})
  self.assertEqual(replay.json()['id'],response.json()['id']);self.assertEqual(self.upstream.await_count,1)

 async def test_stream_terminal_at_eof_preserves_text_and_receipt(self):
  created={'event_type':'interaction.created','interaction':{'id':'stream-exact','object':'interaction','status':'in_progress'}}
  delta={'event_type':'step.delta','index':0,'delta':{'type':'text','text':'Hello streamed'}}
  done={'event_type':'interaction.completed','interaction':{**self.upstream.return_value,'id':'stream-exact'}}
  done['interaction'].pop('steps')
  wire='\n\n'.join('data: '+json.dumps(x) for x in (created,delta,done))
  RealClient=httpx.AsyncClient
  transport=httpx.MockTransport(lambda req:httpx.Response(200,text=wire,headers={'content-type':'text/event-stream'}))
  with patch.object(native,'httpx',SimpleNamespace(AsyncClient=lambda **kw:RealClient(transport=transport,**kw))):
   response=await self.client.post('/v1/interactions',json={'model':'gemini-omni-1.1-flash','input':'hello','stream':True})
  self.assertEqual(response.status_code,200)
  event=[line for line in response.text.splitlines() if line.startswith('data: {"id": "interaction_')][0]
  identifier=json.loads(event[6:])['id'];result=await self.client.get('/v1/interactions/'+identifier)
  self.assertEqual(result.json()['steps'][0]['content'][0]['text'],'Hello streamed')
  self.assertEqual(result.json()['status'],'completed');self.account.finish.assert_awaited_once()

 async def test_terminal_receipt_failure_recovers_without_paid_retry(self):
  self.account.observe.side_effect=RuntimeError('temporary journal outage')
  first=await self.client.post('/v1/interactions',json={'model':'gemini-omni-1.1-flash','input':'hello'})
  self.assertEqual(first.status_code,200);self.account.observe.side_effect=None
  recovered=await self.client.get('/v1/interactions/'+first.json()['id'])
  self.assertEqual(recovered.json()['status'],'completed');self.assertEqual(self.upstream.await_count,1)
  self.account.finish.assert_awaited_once()

 async def post_stream_wire(self,wire,status=200):
  RealClient=httpx.AsyncClient
  transport=httpx.MockTransport(lambda req:httpx.Response(status,text=wire,headers={'content-type':'text/event-stream'}))
  with patch.object(native,'httpx',SimpleNamespace(AsyncClient=lambda **kw:RealClient(transport=transport,**kw),TimeoutException=httpx.TimeoutException,NetworkError=httpx.NetworkError)):
   response=await self.client.post('/v1/interactions',json={'model':'gemini-omni-1.1-flash','input':'hello','stream':True},headers={'Idempotency-Key':uuid4().hex})
  values=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {')]
  values=[value for value in values if str(value.get('id','')).startswith('interaction_')]
  return values[-1]

 async def test_stream_http_rejection_is_failed_but_ambiguous_server_error_is_unknown(self):
  wire='event: error\ndata: '+json.dumps({'event_type':'error','error':{'code':'invalid_request','message':'Invalid argument'}})+'\n\n'
  for status,expected in ((400,'failed'),(429,'failed'),(503,'outcome_unknown')):
   with self.subTest(status=status):
    before=self.account.finish.await_count
    result=await self.post_stream_wire(wire,status)
    self.assertEqual(result['status'],expected)
    self.assertEqual(self.account.finish.await_count,before+(expected=='failed'))
    reread=await self.client.get('/v1/interactions/'+result['id'])
    self.assertEqual(reread.json()['status'],expected)
  self.upstream.assert_not_awaited()

 async def test_stream_error_retains_partial_text_and_observed_usage(self):
  events=[{'event_type':'step.start','index':0,'step':{'type':'model_output','content':[]}},
          {'event_type':'step.delta','index':0,'delta':{'type':'text','text':'Retained partial'}},
          {'event_type':'error','error':{'code':'invalid_request','message':'Rejected'},'usage':{'total_input_tokens':2}}]
  result=await self.post_stream_wire('\n\n'.join('data: '+json.dumps(e) for e in events))
  self.assertEqual(result['status'],'failed')
  self.assertEqual(result['steps'][0]['content'][0]['text'],'Retained partial')
  self.assertEqual(self.account.observe.await_args.kwargs['raw_usage'],{'total_input_tokens':2})
  self.account.finish.assert_awaited_once()

 async def test_incomplete_terminal_retains_output_and_settles_once_without_poll(self):
  self.upstream.return_value={**self.upstream.return_value,'status':'incomplete'}
  first=await self.client.post('/v1/interactions',json={'model':'gemini-omni-1.1-flash','input':'hello','generation_config':{'max_output_tokens':16}})
  result=await self.client.get('/v1/interactions/'+first.json()['id'])
  self.assertEqual(result.json()['status'],'incomplete')
  self.assertEqual(result.json()['steps'],self.upstream.return_value['steps'])
  self.assertEqual(self.upstream.await_count,1);self.account.observe.assert_awaited_once();self.account.finish.assert_awaited_once()

 async def test_status_poll_does_not_erase_partial_text(self):
  self.upstream.return_value={'id':'provider-exact','status':'in_progress','steps':[{'type':'model_output','content':[{'type':'text','text':'Partial'}]}]}
  first=await self.client.post('/v1/interactions',json={'model':'gemini-omni-1.1-flash','input':'hello','background':True})
  self.upstream.return_value={'id':'provider-exact','status':'in_progress'}
  result=await self.client.get('/v1/interactions/'+first.json()['id'])
  self.assertEqual(result.json()['steps'][0]['content'][0]['text'],'Partial')

class GeminiVideoInput(unittest.TestCase):
 def test_pinned_sdk_preserves_signed_video_uri_order_and_mime(self):
  from litellm.llms.vertex_ai.gemini.transformation import _gemini_convert_messages_with_history
  url='https://assets.example.test/source.mp4?token=fixture&expires=123'
  for model in ('gemini-3.1-flash-image','gemini-3.1-flash-lite-image'):
   result=_gemini_convert_messages_with_history([{'role':'user','content':[{'type':'text','text':'Create a poster'},{'type':'file','file':{'file_id':url,'format':'video/mp4'}}]}],model=model,custom_llm_provider='vertex_ai')
   self.assertEqual(result[0]['parts'][0]['text'],'Create a poster')
   self.assertEqual(result[0]['parts'][1]['file_data'],{'file_uri':url,'mime_type':'video/mp4'})

class NativeBoundaries(unittest.IsolatedAsyncioTestCase):
 async def test_supported_webm_keeps_original_bytes_and_legacy_contract(self):
  from generation_job_models import MediaInput
  media=MediaInput(type='video',url='https://fixture.test/original.webm')
  with patch('generation_job_adapters._download_media',AsyncMock(return_value=('original.webm',b'original-webm','video/webm'))):
   content,raw=await native.VertexAdapter._omni_media_content(media,None,expanded=True)
   self.assertEqual(raw,b'original-webm');self.assertEqual(content['mime_type'],'video/webm')
   with self.assertRaises(native.ProviderAdapterError):await native.VertexAdapter._omni_media_content(media,None)
 async def test_count_tokens_exact_alias_and_private_storage_boundary(self):
  app=FastAPI();app.include_router(native.router);app.dependency_overrides[native.user_api_key_auth]=lambda:SimpleNamespace()
  with patch.object(native,'permitted',AsyncMock()),patch.object(native.VertexAdapter,'_vertex_headers',AsyncMock(return_value={})),patch.object(native,'_json_request',AsyncMock(return_value={'totalTokens':7})) as post,patch.dict(os.environ,{'GOOGLE_CLOUD_PROJECT':'fixture'}):
   async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://gateway') as client:
    result=await client.post('/v1/models/gemini-omni-1.1-flash/count-tokens',json={'contents':[{'role':'user','parts':[{'text':'hello'}]}]})
    self.assertEqual(result.status_code,200,result.text);self.assertEqual(result.json()['totalTokens'],7)
    self.assertTrue(post.await_args.args[1].endswith('/gemini-omni-1.1-flash-preview:countTokens'))
    denied=await client.post('/v1/models/gemini-omni-1.1-flash/count-tokens',json={'contents':[{'parts':[{'fileData':{'fileUri':'gs://private/object','mimeType':'video/mp4'}}]}]})
    self.assertEqual(denied.status_code,422);self.assertEqual(post.await_count,1)

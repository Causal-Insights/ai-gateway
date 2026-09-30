"""Finite Vertex Interactions text surface with owned, recoverable identities."""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
from uuid import uuid4
from urllib.parse import quote
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth, UserAPIKeyAuth
from generation_job_adapters import VertexAdapter, ProviderAdapterError, RETRYABLE_HTTP_STATUSES, _json_request
from generation_job_models import MediaInput
from generation_job_repository import repository
from voice_resources import owner_for
from cost_accounting import accounting, identity_for
from gateway_accounting import check_context, finalize_reservation, state
from pricing_registry import registry
from video_capabilities import OMNI

router = APIRouter(tags=['interactions'])
log = logging.getLogger('ai_gateway.interactions')
TERMINAL = {'completed', 'failed', 'cancelled', 'incomplete'}


def fail(status, code, message):
    raise HTTPException(status, detail={'code':code,'message':message})


async def permitted(user, model):
    from litellm.proxy.auth.auth_checks import can_key_call_resolved_model
    from litellm.proxy import proxy_server
    await can_key_call_resolved_model(model=model,llm_model_list=proxy_server.llm_model_list,
        valid_token=user,llm_router=proxy_server.llm_router)


async def owned(public_id, owner):
    pool = await repository.pool()
    row = await pool.fetchrow('select * from gateway_vertex_interactions where id=$1 and app_id=$2 and owner_id=$3 and deleted_at is null', public_id,*owner)
    if not row: fail(404,'INTERACTION_NOT_FOUND','Interaction not found.')
    return row


async def payload(data, owner):
    if not isinstance(data,dict) or data.get('model') not in OMNI:
        fail(422,'INTERACTION_MODEL_UNSUPPORTED','Select an exact supported Gemini Omni model.')
    allowed={'model','input','previous_interaction_id','response_format','stream','background','store','generation_config'}
    if set(data)-allowed:
        fail(422,'INTERACTION_FIELD_UNSUPPORTED','Unsupported Interactions field: '+', '.join(sorted(set(data)-allowed)))
    formats=data.get('response_format',{'type':'text'})
    if formats not in ({'type':'text'},[{'type':'text'}]):
        fail(422,'INTERACTION_FORMAT_UNSUPPORTED','This route exposes Omni text output; use durable generation jobs for video output.')
    body={'stream':False,'background':False,**data,'model':OMNI[data['model']],
          'response_format':{'type':'text','mime_type':'text/plain'},'store':True}
    for key in ('stream','background','store'):
        if key in data and type(data[key]) is not bool:fail(422,'INTERACTION_INPUT_INVALID',key+' must be boolean.')
    if data.get('store') is False:body['store']=False
    if data.get('background') and not body['store']:fail(422,'INTERACTION_INPUT_INVALID','Background interactions require storage.')
    contents=data.get('input')
    if not isinstance(contents,(str,list)) or not contents:fail(422,'INTERACTION_INPUT_INVALID','Provide text or ordered media inputs.')
    if isinstance(contents,list):
        result=[]
        for item in contents:
            if not isinstance(item,dict):fail(422,'INTERACTION_INPUT_INVALID','Inputs must be content objects.')
            if item.get('type')=='text' and isinstance(item.get('text'),str):
                result.append({'type':'text','text':item['text']})
            elif item.get('type') in {'image','video'} and isinstance(item.get('uri'),str):
                # Existing bounded downloader resolves HTTPS bytes; clients do
                # not gain access to the Gateway service account's GCS objects.
                media=MediaInput(type=item['type'],url=item['uri'])
                content,_=await VertexAdapter._omni_media_content(media,None,expanded=True)
                result.append(content)
            else:fail(422,'INTERACTION_INPUT_INVALID','Omni accepts text, image and video content.')
        body['input']=result
    config=data.get('generation_config') or {}
    if not isinstance(config,dict) or set(config)-{'temperature','top_p','seed','stop_sequences','thinking_level','max_output_tokens'}:
        fail(422,'INTERACTION_CONFIG_UNSUPPORTED','Unsupported Omni text generation setting.')
    if data.get('previous_interaction_id'):
        prior=await owned(data['previous_interaction_id'],owner)
        if prior['model']!=data['model'] or not prior['provider_id']:
            fail(409,'INTERACTION_NOT_READY','The previous interaction is not ready for this model.')
        body['previous_interaction_id']=prior['provider_id']
    return body


def public(row, data=None):
    stored=row['response']
    if isinstance(stored,str):stored=json.loads(stored)
    data=data if data is not None else stored or {}
    return {**data,'id':row['id'],'provider_request_id':row['provider_id'],'accounting_id':row['accounting_id'],
            'status':data.get('status',row['state'])}


async def save(row, data):
    pool=await repository.pool()
    retained=row['response'] or {}
    if isinstance(retained,str):retained=json.loads(retained)
    if 'steps' not in data and 'steps' in retained:
        data={**data,'steps':retained['steps']}
    provider_id=data.get('id') or row['provider_id']
    status=data.get('status','in_progress')
    row=await pool.fetchrow('update gateway_vertex_interactions set provider_id=$2,state=$3,response=$4::jsonb,updated_at=now() where id=$1 and deleted_at is null returning *',row['id'],provider_id,status,json.dumps(data))
    if not row:fail(410,'INTERACTION_DELETED','Interaction was deleted.')
    if status in TERMINAL and not row['receipt_projected']:
        try:
            await accounting.observe(row['accounting_id']+':1',raw_usage=data.get('usage') or {},served_model=data.get('model') or OMNI[row['model']],provider_request_id=provider_id,outcome='success' if status=='completed' else 'failure')
            await accounting.finish(row['accounting_id'])
            await finalize_reservation(state.get() or {})
            row=await pool.fetchrow('update gateway_vertex_interactions set receipt_projected=true where id=$1 returning *',row['id'])
        except Exception:
            log.exception('interaction_receipt_pending',extra={'accounting_id':row['accounting_id']})
    return row


async def fetch(row):
    if not row['provider_id']:return row
    data=await _json_request('GET',VertexAdapter._omni_url(quote(row['provider_id'],safe='')),headers=await VertexAdapter._vertex_headers())
    return await save(row,data)


@router.post('/v1/interactions')
async def create(request:Request,user:UserAPIKeyAuth=Depends(user_api_key_auth)):
    owner=owner_for(user,request.headers)
    data=await request.json();await permitted(user,data.get('model') if isinstance(data,dict) else None)
    body=await payload(data,owner)
    request_id=request.headers.get('idempotency-key') or str(uuid4())
    fingerprint=hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    pool=await repository.pool()
    row=await pool.fetchrow('''insert into gateway_vertex_interactions(id,app_id,owner_id,request_id,request_hash,model)
        values($1,$2,$3,$4,$5,$6) on conflict(app_id,owner_id,request_id) do nothing returning *''','interaction_'+uuid4().hex,*owner,request_id,fingerprint,data['model'])
    if not row:
        row=await pool.fetchrow('select * from gateway_vertex_interactions where app_id=$1 and owner_id=$2 and request_id=$3',*owner,request_id)
        if row['request_hash']!=fingerprint:fail(409,'IDEMPOTENCY_CONFLICT','This request ID contains different instructions.')
        return JSONResponse(public(row),status_code=200 if row['state']=='completed' else 202)
    identity=identity_for(user)
    try:
        profile=registry().select(data['model'],'interactions',{'tools':False})
        check_context(profile);await accounting.check_budgets(identity,data['model'])
        aid=await accounting.begin(model=data['model'],route='interactions',identity=identity,accounting_id=row['id'])
        await accounting.attempt(aid,profile,attempt_id=aid+':1')
        row=await pool.fetchrow('update gateway_vertex_interactions set accounting_id=$2 where id=$1 returning *',row['id'],aid)
        current=state.get()
        if current is not None:current.update(accounting_id=aid,profile=profile,alias=data['model'],route='interactions',attempts=[aid+':1'],reservation=getattr(user,'budget_reservation',None))
        if body.get('stream'):
            return StreamingResponse(stream(row,body),media_type='text/event-stream',headers={'X-Gateway-Accounting-Id':aid})
        result=await _json_request('POST',VertexAdapter._omni_url(),headers=await VertexAdapter._vertex_headers(),body=body,submission=True)
        row=await save(row,result)
        return public(row,result)
    except ProviderAdapterError as exc:
        if not exc.outcome_unknown:
            result={'status':'failed','error':{'code':exc.code,'message':str(exc)}}
            row=await save(row,result)
            return JSONResponse(public(row,result),status_code=exc.status_code or 502)
        await pool.execute("update gateway_vertex_interactions set state='outcome_unknown',updated_at=now() where id=$1 and state='creating'",row['id'])
        return JSONResponse(public(row,{'status':'outcome_unknown','error':{'code':exc.code,'message':str(exc)}}),status_code=502)
    except Exception:
        await pool.execute("update gateway_vertex_interactions set state='outcome_unknown',updated_at=now() where id=$1 and state='creating'",row['id'])
        raise


async def stream_lines(response):
    async for line in response.aiter_lines():
        yield line
    yield ""  # A valid final event can end at EOF without a blank line.


async def stream(row,body):
    """Forward provider events; persist identity early for disconnect recovery."""
    steps={}

    async def retain_error(error, *, definitive, usage=None):
        prior=row['response'] or {}
        if isinstance(prior,str):prior=json.loads(prior)
        data={**prior,'status':'failed' if definitive else 'outcome_unknown','error':error}
        if steps:data['steps']=[steps[key] for key in sorted(steps)]
        if usage is not None:data['usage']=usage
        return await save(row,data)

    try:
        async with httpx.AsyncClient(timeout=600) as client:
            async with client.stream('POST',VertexAdapter._omni_url(),headers=await VertexAdapter._vertex_headers(),json=body) as response:
                if response.is_error:
                    await response.aread()
                    try:error_data=response.json()
                    except ValueError:
                        error_data={'error':{'code':'PROVIDER_HTTP_ERROR','message':response.text[:2000]}}
                        for line in response.text.splitlines():
                            if line.startswith('data:'):
                                try:event_data=json.loads(line[5:].strip())
                                except ValueError:continue
                                if isinstance(event_data,dict) and event_data.get('error'):
                                    error_data=event_data;break
                    definitive=response.status_code not in RETRYABLE_HTTP_STATUSES or response.status_code==429
                    row=await retain_error(error_data.get('error') or error_data,definitive=definitive,usage=error_data.get('usage'))
                    yield ('event: gateway.interaction\ndata: '+json.dumps(public(row))+'\n\n').encode()
                    return
                event=[]
                provider_error=False
                async for line in stream_lines(response):
                    if line:
                        event.append(line)
                        continue
                    if not event:continue
                    value='\n'.join(line[5:].lstrip() for line in event if line.startswith('data:'))
                    try:obj=json.loads(value)
                    except ValueError:obj={}
                    if obj.get('event_type')=='error':
                        error=obj.get('error') or {'code':'PROVIDER_STREAM_ERROR','message':'Provider stream failed.'}
                        # A transport/internal error can follow paid execution.
                        # Only an explicit request rejection establishes failure.
                        definitive=error.get('code') in {'invalid_request','invalid_argument','permission_denied','unauthenticated','resource_exhausted'}
                        if row['state'] not in TERMINAL:
                            row=await retain_error(error,definitive=definitive,usage=obj.get('usage'))
                        yield ('\n'.join(event)+'\n\n').encode()
                        provider_error=True
                        break
                    if obj.get('event_type')=='step.start':
                        steps[obj.get('index',0)]=obj.get('step') or {}
                    if obj.get('event_type')=='step.delta' and (obj.get('delta') or {}).get('type')=='text':
                        step=steps.setdefault(obj.get('index',0),{'type':'model_output'})
                        content=step.setdefault('content',[{'type':'text','text':''}])
                        if not content:content.append({'type':'text','text':''})
                        content[0]['text']=content[0].get('text','')+obj['delta'].get('text','')
                    interaction=obj.get('interaction') or (obj if obj.get('object')=='interaction' else {})
                    if interaction.get('id'):
                        if steps and not interaction.get('steps'):
                            interaction={**interaction,'steps':[steps[key] for key in sorted(steps)]}
                        row=await save(row,interaction)
                        if interaction.get('status')=='in_progress':
                            yield ('event: gateway.interaction.created\ndata: '+json.dumps({'id':row['id'],'accounting_id':row['accounting_id']})+'\n\n').encode()
                    # Provider identity remains explicit, while clients receive
                    # the owned handle separately for future reads/continuation.
                    yield ('\n'.join(event)+'\n\n').encode();event=[]
                if not provider_error and row['provider_id'] and row['state'] not in TERMINAL:
                    row=await fetch(row)
                yield ('event: gateway.interaction\ndata: '+json.dumps(public(row))+'\n\n').encode()
    except (httpx.TimeoutException,httpx.NetworkError):
        row=await retain_error({'code':'SUBMISSION_OUTCOME_UNKNOWN','message':'Provider stream ended without a definitive result.'},definitive=False)
        yield ('event: gateway.interaction\ndata: '+json.dumps(public(row))+'\n\n').encode()
    finally:
        if steps and row["state"] not in TERMINAL:
            prior=row["response"] or {}
            if isinstance(prior,str):prior=json.loads(prior)
            await asyncio.shield(save(row,{**prior,"steps":[steps[key] for key in sorted(steps)]}))
        pool=await repository.pool()
        await asyncio.shield(pool.execute("update gateway_vertex_interactions set state='outcome_unknown',updated_at=now() where id=$1 and state='creating'",row['id']))


@router.get('/v1/interactions/{interaction_id}')
async def get(interaction_id:str,request:Request,user:UserAPIKeyAuth=Depends(user_api_key_auth)):
    row=await owned(interaction_id,owner_for(user,request.headers));await permitted(user,row['model'])
    if row['provider_id'] and row['state'] not in TERMINAL:row=await fetch(row)
    elif row['state'] in TERMINAL and not row['receipt_projected']:
        retained=row['response'];row=await save(row,json.loads(retained) if isinstance(retained,str) else retained)
    return public(row)


@router.delete('/v1/interactions/{interaction_id}')
async def delete(interaction_id:str,request:Request,user:UserAPIKeyAuth=Depends(user_api_key_auth)):
    row=await owned(interaction_id,owner_for(user,request.headers));await permitted(user,row['model'])
    if row['provider_id']:
        async with httpx.AsyncClient(timeout=60) as client:
            response=await client.delete(VertexAdapter._omni_url(quote(row['provider_id'],safe='')),headers=await VertexAdapter._vertex_headers())
        if response.status_code not in {200,204,404}:fail(502,'INTERACTION_DELETE_FAILED','Provider deletion failed; retry deletion without resubmitting generation.')
    pool=await repository.pool()
    await pool.execute("update gateway_vertex_interactions set state='deleted',response=null,deleted_at=now() where id=$1",row['id'])
    return {'id':row['id'],'deleted':True}

@router.post('/v1/models/{model}/count-tokens')
async def count_tokens(model:str,request:Request,user:UserAPIKeyAuth=Depends(user_api_key_auth)):
    """Exact configured Vertex models, finite CountTokens operation."""
    await permitted(user,model)
    entry=registry().models.get(model) or {}
    upstream=entry.get('upstream_model','')
    if entry.get('disabled') or not upstream.startswith('vertex_ai/gemini-'):
        fail(422,'TOKEN_COUNT_MODEL_UNSUPPORTED','Native token counting requires a configured Gemini model.')
    data=await request.json()
    if not isinstance(data,dict) or set(data)-{'contents','systemInstruction','tools','generationConfig'} or not isinstance(data.get('contents'),list):
        fail(422,'TOKEN_COUNT_INPUT_INVALID','Provide native Gemini contents and optional systemInstruction, tools or generationConfig.')
    # A new endpoint must not expose service-account access to private GCS paths.
    for content in data['contents']:
        for part in content.get('parts',[]) if isinstance(content,dict) else []:
            file_data=part.get('fileData',part.get('file_data',{})) if isinstance(part,dict) else {}
            uri=file_data.get('fileUri',file_data.get('file_uri',''))
            if uri and not uri.startswith('https://'):fail(422,'TOKEN_COUNT_MEDIA_INVALID','Use HTTPS media or inline data.')
    import os
    project=os.environ.get('GOOGLE_CLOUD_PROJECT','')
    endpoint=f'https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/publishers/google/models/{quote(upstream.split("/",1)[1],safe="")}:countTokens'
    return await _json_request('POST',endpoint,headers=await VertexAdapter._vertex_headers(),body=data)

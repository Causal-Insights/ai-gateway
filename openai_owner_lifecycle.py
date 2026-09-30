"""Owner revocation and resumable finite provider-resource cleanup.

Accounting rows survive deletion. Provider Batch has cancellation, not deletion;
its input/output files are removed only after available item usage is observed.
"""
from contextvars import ContextVar
import functools
import importlib
from urllib.parse import quote

_cleaning = ContextVar('openai_owner_cleanup', default=False)
_owner_data_budget = ContextVar('openai_owner_data_budget', default=False)
COLLECTIONS = {'batch': 'batches', 'response': 'responses', 'file': 'files',
               'vector_store': 'vector_stores', 'container': 'containers'}


def install_budget_exemption():
    """Pinned SDK admission still authenticates owner cleanup without a paid hold."""
    auth=importlib.import_module('litellm.proxy.auth.user_api_key_auth')
    original=auth._should_skip_budget_checks
    if getattr(original,'_gateway_owner_cleanup',False):return
    @functools.wraps(original)
    def skip(*,request_data,route,request,llm_router):
        if request is not None and request.url.path == '/v1/resources/owner-data' and request.method in {'GET','DELETE'}:
            return True
        return original(request_data=request_data,route=route,request=request,llm_router=llm_router)
    skip._gateway_owner_cleanup=True
    auth._should_skip_budget_checks=skip
    # The virtual-key path checks its own ceiling before common_checks. Its
    # callback lacks request context, so use the exact-route middleware scope.
    original_key_budget=auth._virtual_key_max_budget_check
    @functools.wraps(original_key_budget)
    async def key_budget(*args,**kwargs):
        if _owner_data_budget.get():return
        return await original_key_budget(*args,**kwargs)
    auth._virtual_key_max_budget_check=key_budget


async def revoked(owner):
    import owned_openai_resources as r
    return await (await r.repository.pool()).fetchval(
        'select 1 from gateway_openai_owner_cleanup where app_id=$1 and owner_id=$2', *owner)


async def ensure_active(owner):
    if await revoked(owner):
        import owned_openai_resources as r
        r.fail(410, 'RESOURCE_OWNER_REVOKED', 'Resource access for this owner has been revoked.')


async def cleanup_late_resource(owner):
    """A provider create admitted before revocation can return afterwards."""
    if not _cleaning.get() and await revoked(owner):
        await owner_data(owner, purge=True)


async def rows_for(owner):
    import owned_openai_resources as r
    return [r.decode(row) for row in await (await r.repository.pool()).fetch(
        'select * from gateway_openai_resources where app_id=$1 and owner_id=$2 order by created_at', *owner)]


async def erase(row):
    import owned_openai_resources as r
    # Keep provider identities for idempotent reconciliation and original
    # accounting IDs, but remove customer payload/metadata and batch manifests.
    await (await r.repository.pool()).execute("""update gateway_openai_resources
        set state='deleted',data=jsonb_strip_nulls(jsonb_build_object(
            'gateway_accounting_id',data->'gateway_accounting_id','gateway_attempt_id',data->'gateway_attempt_id')),
            manifest=null,updated_at=now() where id=$1""", row['id'])


async def execution(row, owner):
    import owned_openai_resources as r
    path='/' + COLLECTIONS[row['kind']] + '/' + quote(row['provider_id'], safe='')
    response=await r.provider('GET', path)
    if response.status_code == 404:
        # Unavailable results do not establish zero cost. Existing unresolved
        # receipts remain available to accounting reconciliation.
        if row['kind']=='batch':
            for item in await (await r.repository.pool()).fetch('select accounting_id from gateway_openai_batch_items where batch_id=$1',row['id']):
                await r.accounting.finish(item['accounting_id'])
        elif row['data'].get('gateway_accounting_id'):
            await r.accounting.finish(row['data']['gateway_accounting_id'])
        await erase(row)
        return None
    body=r.upstream_ok(response).json()
    terminal=r.TERMINAL if row['kind']=='batch' else {'completed','failed','incomplete','cancelled'}
    if body.get('status') not in terminal:
        # Repeated cancel is avoidable while Batch is already cancelling.
        if body.get('status')!='cancelling':
            body=r.upstream_ok(await r.provider('POST',path+'/cancel')).json()
    if row['kind']=='batch':
        row=await r.save(row,'ready',body)
        await r.settle_batch(row,owner)
    else:
        await r.settle_response(row,body,owner)
    if body.get('status') not in terminal:
        return 'cancellation_pending'
    if row['kind']=='response':
        deleted=await r.provider('DELETE',path)
        if deleted.status_code!=404:r.upstream_ok(deleted)
    await erase(row)
    return None


async def sweep(owner):
    import owned_openai_resources as r
    pending=[]
    # Drain work before removing files needed for accounting or provider input.
    for row in await rows_for(owner):
        if row['state']=='deleted' or row['kind'] not in {'batch','response'}:continue
        try:
            if not row['provider_id']:
                if row['state']=='failed':
                    await erase(row);continue
                reason='provider_identity_unknown'
            else:reason=await execution(row,owner)
        except Exception:
            r.log.exception('owner_execution_cleanup_pending',extra={'resource_id':row['id']})
            reason='receipt_or_provider_pending'
        if reason:pending.append({'kind':row['kind'],'id':row['id'],'reason':reason})
    if pending:return pending
    # Receipt processing can discover output files and containers, so reread.
    for kind in ('vector_store','container','file'):
        for row in await rows_for(owner):
            if row['kind']!=kind or row['state']=='deleted':continue
            try:
                if not row['provider_id']:
                    if row['state']=='failed':await erase(row);continue
                    pending.append({'kind':kind,'id':row['id'],'reason':'provider_identity_unknown'});continue
                response=await r.provider('DELETE','/'+COLLECTIONS[kind]+'/'+quote(row['provider_id'],safe=''))
                if response.status_code!=404:r.upstream_ok(response)
                await erase(row)
            except Exception:
                r.log.exception('owner_resource_cleanup_pending',extra={'resource_id':row['id']})
                pending.append({'kind':kind,'id':row['id'],'reason':'provider_delete_pending'})
    return pending


async def owner_data(owner, *, purge=False):
    import owned_openai_resources as r
    pool=await r.repository.pool()
    pending=[]
    if purge:
        await pool.execute('''insert into gateway_openai_owner_cleanup(app_id,owner_id) values($1,$2)
            on conflict(app_id,owner_id) do update set updated_at=now()''',*owner)
        token=_cleaning.set(True)
        try:pending=await sweep(owner)
        finally:_cleaning.reset(token)
    is_revoked=bool(await revoked(owner))
    rows=await rows_for(owner)
    if is_revoked and not purge:
        pending=[{'kind':row['kind'],'id':row['id'],'reason':'cleanup_pending'} for row in rows if row['state']!='deleted']
    fields={'filename','purpose','bytes','name','status','expires_at','created_at','gateway_accounting_id'}
    exported=[{'id':row['id'],'provider_id':row['provider_id'],'kind':row['kind'],'state':row['state'],
               'metadata':{key:value for key,value in row['data'].items() if key in fields}}
              for row in rows] if not purge else []
    receipts=[{'resource_id':row['batch_id'],'custom_id':row['custom_id'],'accounting_id':row['accounting_id']}
              for row in await pool.fetch('''select i.* from gateway_openai_batch_items i join gateway_openai_resources r
                  on r.id=i.batch_id where r.app_id=$1 and r.owner_id=$2 order by i.batch_id,i.custom_id''',*owner)]
    return {'revoked':is_revoked,'cleanup_status':'pending' if pending else 'complete' if is_revoked else 'not_requested',
            'pending':pending,'resources':exported,'receipts':receipts}

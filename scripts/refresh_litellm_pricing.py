#!/usr/bin/env python3
"""Capture resolved LiteLLM prices. Review the diff; no deployment or paid calls."""
import argparse,copy,hashlib,json,sys
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

def main():
    import litellm
    from litellm_pricing import profile_for,catalog
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('map_json',type=Path,help='Explicitly downloaded LiteLLM model_prices_and_context_window.json')
    parser.add_argument('--source',default='https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json')
    args=parser.parse_args()
    content=args.map_json.read_bytes();source=json.loads(content)
    litellm.model_cost.clear();litellm.model_cost.update(source)
    old=catalog();snapshot={'source':args.source,'retrieved_at':datetime.now(timezone.utc).isoformat(),'source_sha256':hashlib.sha256(content).hexdigest(),'models':{},'entries':{}}
    path=ROOT/'pricing/registry.json';registry=json.loads(path.read_text());unresolved=[]
    for alias,model in registry['models'].items():
        upstream=model['upstream_model'];provider,name=upstream.split('/',1)
        if provider in {'grok-image','grok-video'}:provider='xai'
        if (provider in {'seedance','seedream','audio-studio'} or 'omni' in name
                or model.get('deployment_model','').startswith('audio-studio/')):continue
        resolver=provider+'/'+name
        try:info=litellm.get_model_info(model=resolver,custom_llm_provider=provider)
        except Exception:unresolved.append(alias);continue
        key=next((k for k in (resolver,name) if k in source),None)
        if not key:
            # Retain the exact resolver result instead of guessing a neighboring model.
            key=resolver
            entry=dict(info)
        else:entry=copy.deepcopy(source[key])
        prior=old.get('entries',{}).get(key,{})
        if prior.get('gateway_rate_evidence'):
            # Demonstrated discrepancies are explicit exceptions until reviewed.
            for k,v in prior.items():
                if k.startswith('gateway_') or ('cost' in k and not prior.get('gateway_video_silent_rates')):entry[k]=v
        snapshot['models'][alias]={'resolver_model':resolver,'catalog_key':key,'provider':provider}
        snapshot['entries'][key]=entry
    if unresolved:raise SystemExit('No price resolution for: '+', '.join(unresolved))
    (ROOT/'pricing/litellm_catalog.json').write_text(json.dumps(snapshot,indent=2)+'\n');catalog.cache_clear()
    for alias in snapshot['models']:
        m=registry['models'][alias];oldp=registry['profiles'][m['profiles'][0]]
        p=profile_for(alias,{**m,'vendor':oldp['vendor']})
        if oldp.get('usage_corrections'):
            p['usage_corrections']=oldp['usage_corrections'];p['correction_reason']=oldp['correction_reason']
            p['version']+='-cache-'+hashlib.sha256(json.dumps(p['usage_corrections'],sort_keys=True).encode()).hexdigest()[:12]
        existing=registry['profiles'].get(p['version'])
        if existing:
            # The same rates retain their original version/effective date.
            p=existing
        registry['profiles'][p['version']]=p;m['profiles']=[p['version']]
    path.write_text(json.dumps(registry,indent=2)+'\n')
    print(f'Captured {len(snapshot["models"])} aliases. Review pricing diffs and run coverage/tests before deployment.')
if __name__=='__main__':main()

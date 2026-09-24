import copy
from decimal import Decimal
import unittest
from pricing_registry import PricingRegistry, PricingError
from litellm_pricing import install_catalog

class CatalogPricingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_catalog()
        cls.prices = PricingRegistry()

    def test_every_configured_alias_has_admission_and_a_measured_cost(self):
        seen = set()
        for alias, model in self.prices.models.items():
            profiles = [self.prices.profiles[v] for v in model['profiles']]
            with self.subTest(alias=alias):
                self.assertFalse(model.get('disabled'))
                p = self.prices.select(alias, profiles[0]['routes'][0])
                if p['version'] in seen:
                    continue
                seen.add(p['version'])
                mode = p.get('model_info',{}).get('mode')
                if mode == 'chat':
                    raw={'input_tokens':100,'output_tokens':20,'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0}}
                elif mode == 'image_generation':
                    raw={'input_tokens':100,'output_tokens':1000,'output_images':1,'input_tokens_details':{'text_tokens':50,'image_tokens':50,'cached_tokens':0},'output_tokens_details':{'text_tokens':0,'image_tokens':1000}}
                elif mode == 'audio_speech': raw={'billable_characters':100}
                elif mode == 'video_generation': raw={'output_video_seconds':4}
                else:raw={c['usage']:100 for c in p['components']}
                served={k: p.get('defaults',{}).get(k,p['options'][k][0]) for k in p.get('require_served_options',[])}
                self.assertGreater(Decimal(self.prices.calculate(p,raw,served_options=served)['cost_usd']),0)

    def test_catalog_has_no_manual_acceptance_gate_or_review_expiry(self):
        p=self.prices.select('gpt-5.4-mini','completion',{'tools':True,'reasoning_effort':'high'})
        self.assertEqual(p['engine'],'litellm')
        self.assertFalse(p['acceptance_evidence'])

    def test_pinned_snapshot_survives_catalog_changes(self):
        import litellm
        p=self.prices.select('gpt-5.4-mini','completion')
        raw={'input_tokens':1000,'output_tokens':100}
        before=self.prices.calculate(p,raw)['cost_usd']
        old=copy.deepcopy(litellm.model_cost.get(p['cost_model']))
        try:
            litellm.register_model({p['cost_model']:{**p['model_info'],'input_cost_per_token':100}})
            self.assertEqual(self.prices.calculate(p,raw)['cost_usd'],before)
        finally:
            if old:litellm.register_model({p['cost_model']:old})

    def test_custom_media_rates_use_actual_counts_and_duration(self):
        cases=[('seedream-5.0',{'output_images':2},'0.070'),('seedream-5.0-lite',{'output_images':2},'0.070'),
          ('seedream-5.0-pro',{'output_images_small':0,'output_images_large':1,'billable_reference_images':2},'0.096'),
          ('elevenlabs-sfx',{'audio_seconds':10},'0.020'),('elevenlabs-music',{'audio_seconds':60},'0.150')]
        for alias,usage,expected in cases:
            with self.subTest(alias=alias):
                p=self.prices.select(alias,'image_generation')
                self.assertEqual(Decimal(self.prices.calculate(p,usage)['cost_usd']),Decimal(expected))
                with self.assertRaises(PricingError):self.prices.calculate(p,{})

    def test_seedance25_uses_modelark_tokens_with_shared_key(self):
        from generation_job_adapters import BytePlusAdapter
        from unittest.mock import patch
        p=self.prices.select('seedance-2.5','generation_job',{'resolution':'720p'})
        result=self.prices.calculate(p,{'completion_tokens':100000},served_options={'resolution':'720p','input_video':False})
        self.assertEqual(Decimal(result['cost_usd']),Decimal('1.07'))
        with patch.dict('os.environ',{'BYTEDANCE_API_KEY':'fixture'},clear=True):
            a=BytePlusAdapter()
            self.assertEqual(a._headers('dreamina-seedance-2-5-260628')['Authorization'],'Bearer fixture')
            self.assertIn('ark.ap-southeast.bytepluses.com',a._base('dreamina-seedance-2-5-260628'))

class ModelArkMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_seedance25_preserves_image_video_and_audio_reference_types(self):
        from unittest.mock import AsyncMock,patch
        from generation_job_adapters import BytePlusAdapter
        from generation_job_models import GenerationJobCreate
        request=GenerationJobCreate(model='seedance-2.5',prompt='Reference-media fixture',duration_seconds=11,
            generate_audio=True,media_inputs=[{'type':kind,'role':'reference','url':'https://example.com/'+kind}
                                            for kind in ['image','image','video','audio']])
        call=AsyncMock(return_value={'id':'task-fixture'})
        with patch.dict('os.environ',{'BYTEDANCE_API_KEY':'fixture'},clear=True),patch('generation_job_adapters._json_request',call):
            result=await BytePlusAdapter().submit(request,job_id='fixture-job',callback_url=None)
        body=call.await_args.kwargs['body']
        self.assertEqual([x['type'] for x in body['content']],['text','image_url','image_url','video_url','audio_url'])
        self.assertEqual(body['content'][-1]['role'],'reference_audio')
        self.assertTrue(result.request_metadata['has_input_video'])
        p=PricingRegistry().select('seedance-2.5','generation_job',{'input_video':True,'resolution':'720p'})
        self.assertEqual(p['components'][0]['rate'],'6.4')

class VideoAudioPricingTests(unittest.TestCase):
    def test_veo_silent_and_audio_outputs_use_different_published_rates(self):
        prices=PricingRegistry()
        for alias,silent,audio in [('veo-3.1','0.8','1.6'),('veo-3.1-fast','0.32','0.4'),('veo-3.1-lite','0.12','0.2')]:
            with self.subTest(alias=alias):
                for enabled,expected in [(False,silent),(True,audio)]:
                    p=prices.select(alias,'generation_job',{'generate_audio':enabled,'resolution':'720p'})
                    result=prices.calculate(p,{'output_video_seconds':4})
                    self.assertEqual(Decimal(result['cost_usd']),Decimal(expected))

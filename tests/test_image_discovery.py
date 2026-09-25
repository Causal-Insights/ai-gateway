"""Discovery follows exact image/native adapters and distinguishes buffered events."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import capability_discovery as discovery
from openai_model_contracts import IMAGE_QUALITIES, IMAGE_PRESERVED_FIELDS
from gateway_request_policy import GROK_IMAGE_2_ASPECT_RATIOS


class ImageDiscoveryTests(unittest.TestCase):
    def test_seedream_buffered_events_are_distinct_from_pro_layers(self):
        for alias in ('seedream-5.0', 'seedream-5.0-lite'):
            contract = discovery.image_contract(alias)
            self.assertFalse(contract['streaming']['live_partial_delivery'])
            self.assertEqual(contract['operations'][0]['execution_modes'], ['sync', 'buffered_stream'])
            self.assertTrue(contract['all_outputs_retained'])
            self.assertEqual(contract['combined_reference_and_output_limit'], 15)
        contract = discovery.image_contract('seedream-5.0-pro')
        self.assertFalse(contract['streaming']['supported'])
        layer = next(item for item in contract['operations'] if item['id'] == 'image.layers')
        self.assertEqual(layer['reference_count'], 1)
        self.assertTrue(layer['prompt_optional'])
        self.assertTrue(layer['all_layers_retained'])
        self.assertIn('auto', layer['settings']['size']['presets'])
        self.assertEqual(layer['settings']['output_format']['values'], ['png'])
        self.assertTrue(all(item['execution_modes'] == ['sync'] for item in contract['operations']))

    def test_gpt_controls_share_exact_contract_and_do_not_claim_flare_stream_verified(self):
        modern = discovery.image_contract('gpt-image-2.5-sunburst')
        self.assertEqual(set(modern['operations'][0]['settings']['quality']['values']), IMAGE_QUALITIES)
        self.assertEqual(modern['preserved_fields'], list(IMAGE_PRESERVED_FIELDS))
        self.assertEqual(modern['operations'][0]['settings']['size']['dimension_multiple'], 16)
        flare = discovery.image_contract('gpt-image-2.5-flare')
        self.assertEqual(flare['operations'][0]['execution_modes'], ['sync'])
        self.assertEqual(flare['streaming']['availability'], 'unverified_provider_documentation_conflict')
        for alias in ('gpt-image-1.5', 'gpt-image-2'):
            self.assertEqual(discovery.image_contract(alias)['operations'][0]['settings']['partial_images'], {'min': 0, 'max': 3})
        self.assertIsNone(discovery.image_contract('gpt-image-unimplemented'))

    def test_exact_grok_aliases_and_gemini_reference_video_eligibility(self):
        for alias in ('grok-image', 'grok-imagine-image-quality', 'grok-imagine-image-2.0'):
            self.assertIsNotNone(discovery.image_contract(alias))
        grok = discovery.image_contract('grok-imagine-image-2.0')['operations'][0]
        self.assertEqual(set(grok['settings']['aspect_ratio']['values']), GROK_IMAGE_2_ASPECT_RATIOS)
        self.assertIsNone(discovery.image_contract('grok-image-future'))
        for alias in ('nano-banana', 'nano-banana-2', 'nano-banana-2-lite'):
            contract = discovery.image_contract(alias)
            self.assertEqual(contract['operations'][0]['endpoint'], '/v1/chat/completions')
            self.assertTrue(contract['reference_video']['supported'])
            self.assertEqual(contract['operations'][0]['settings']['modalities']['values'], [['text', 'image']])
            fields = contract['operations'][0]['settings']['extra_body.generationConfig.imageConfig']['fields']
            self.assertEqual(fields, ['aspectRatio'] if alias == 'nano-banana-2-lite' else ['aspectRatio', 'imageSize'])
            self.assertEqual(contract['reference_video']['wire']['file']['format'], 'video/mp4')
        self.assertFalse(discovery.image_contract('nano-banana-pro')['reference_video']['supported'])

    def test_omni_text_and_token_count_extend_existing_video_discovery(self):
        models = {'gemini-omni-1.1-flash': {'upstream_model': 'vertex_ai/gemini-omni-1.1-flash-preview'},
                  'nano-banana-pro': {'upstream_model': 'vertex_ai/gemini-3-pro-image'},
                  'gpt-image-2': {'upstream_model': 'openai/gpt-image-2'}}
        with patch.object(discovery, 'registry', return_value=SimpleNamespace(models=models, document={})), patch.object(discovery, 'discovery_contract', return_value=None):
            found = discovery.document()['models']
        omni = {item['id']: item for item in found['gemini-omni-1.1-flash']['operations']}
        self.assertIn('generate.text', omni)
        self.assertTrue(omni['text.interaction']['owner_delegation_required'])
        self.assertEqual(omni['text.interaction']['media_roles'], ['text', 'image', 'video'])
        self.assertIn('tokens.count', omni)
        self.assertIn('tokens.count', {item['id'] for item in found['nano-banana-pro']['operations']})
        self.assertNotIn('tokens.count', {item['id'] for item in found['gpt-image-2']['operations']})

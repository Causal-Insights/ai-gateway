import copy
from decimal import Decimal
import unittest
from unittest.mock import patch

from pricing_registry import PricingError, PricingRegistry
from litellm_pricing import profile_for, install_catalog, catalog


class GeminiImageRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_catalog()
        cls.prices = PricingRegistry()

    def test_every_nano_alias_prices_chat_image_generation_and_edit_routes(self):
        usage = {'input_tokens': 100, 'output_tokens': 1000, 'output_images': 1,
                 'input_tokens_details': {'text_tokens': 50, 'image_tokens': 50, 'cached_tokens': 0},
                 'output_tokens_details': {'text_tokens': 0, 'image_tokens': 1000}}
        for alias in ('nano-banana', 'nano-banana-2', 'nano-banana-pro', 'nano-banana-2-lite'):
            with self.subTest(alias=alias):
                chat = self.prices.select(alias, 'completion')
                edit = self.prices.select(alias, 'image_edit')
                generated = self.prices.select(alias, 'image_generation')
                self.assertEqual(chat['version'], edit['version'])
                self.assertEqual(chat['version'], generated['version'])
                cost = self.prices.calculate(chat, usage)['cost_usd']
                self.assertGreater(Decimal(cost), 0)
                self.assertEqual(cost, self.prices.calculate(edit, usage)['cost_usd'])
                chat_route_version = chat['version'].removesuffix('-grounding-20260924')
                chat_route = self.prices.profiles[chat_route_version]
                original_version = chat_route_version.removesuffix('-chat-route-v1')
                original = self.prices.profiles[original_version]
                current = {k: v for k, v in chat_route.items() if k not in {'version', 'routes', 'effective_from', 'request_options'}}
                historical = {k: v for k, v in original.items() if k not in {'version', 'routes', 'effective_from', 'request_options'}}
                self.assertEqual(current, historical)
                self.assertNotIn('completion', original['routes'])
                self.assertEqual(cost, self.prices.calculate(original, usage)['cost_usd'])

    def test_catalog_refresh_retains_gemini_chat_route_and_version(self):
        for alias in ('nano-banana', 'nano-banana-2', 'nano-banana-pro', 'nano-banana-2-lite'):
            with self.subTest(alias=alias):
                selected = self.prices.select(alias, 'completion')
                refreshed = profile_for(alias, {**self.prices.models[alias], 'vendor': selected['vendor']}, previous=selected)
                self.assertEqual(refreshed['routes'], selected['routes'])
                self.assertEqual(refreshed['version'], selected['version'])
                self.assertEqual(refreshed['model_info'], selected['model_info'])
                self.assertEqual(refreshed.get('grounding_tariff'), selected.get('grounding_tariff'))

    def test_changed_token_catalog_preserves_independent_grounding_tariff_and_history(self):
        selected = self.prices.select('nano-banana', 'completion')
        historical = copy.deepcopy(selected)
        updated = copy.deepcopy(catalog())
        key = updated['models']['nano-banana']['catalog_key']
        updated['entries'][key]['input_cost_per_token'] *= 2
        with patch('litellm_pricing.catalog', return_value=updated):
            refreshed = profile_for('nano-banana', {**self.prices.models['nano-banana'], 'vendor': selected['vendor']}, previous=selected)
        self.assertNotEqual(refreshed['version'], selected['version'])
        self.assertEqual(refreshed['grounding_tariff'], selected['grounding_tariff'])
        self.assertEqual(refreshed['evidence'], selected['evidence'])
        self.assertIn('completion', refreshed['routes'])
        self.assertEqual(selected, historical)

    def test_grounded_cached_text_refresh_retains_modifier_order_and_version(self):
        for alias, model in self.prices.models.items():
            selected = self.prices.profiles[model['profiles'][0]]
            if not selected.get('grounding_tariff'):
                continue
            with self.subTest(alias=alias):
                refreshed = profile_for(alias, {**model, 'vendor': selected['vendor']}, previous=selected)
                self.assertEqual(refreshed['version'], selected['version'])
                self.assertEqual(refreshed.get('usage_corrections'), selected.get('usage_corrections'))
                self.assertEqual(refreshed['grounding_tariff'], selected['grounding_tariff'])

    def test_openai_image_models_do_not_gain_an_unsupported_chat_route(self):
        for alias in ('gpt-image-2', 'gpt-image-2.5-flare', 'gpt-image-2.5-sunburst'):
            with self.subTest(alias=alias):
                with self.assertRaises(PricingError):
                    self.prices.select(alias, 'completion')


if __name__ == '__main__':
    unittest.main()

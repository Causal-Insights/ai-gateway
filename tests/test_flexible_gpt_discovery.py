import unittest
from types import SimpleNamespace
from unittest.mock import patch
import capability_discovery as discovery

class FlexibleGptDiscoveryTests(unittest.TestCase):
    def test_exact_flexible_aliases_expose_efforts_without_changing_fixed_presets(self):
        models = {alias: {'upstream_model': 'openai/'+alias, 'profiles': []} for alias in ['gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna','gpt-5.6-sol-medium']}
        with patch.object(discovery, 'registry', return_value=SimpleNamespace(models=models, profiles={},document={})), patch.object(discovery, 'discovery_contract', return_value=None):
            result=discovery.document()['models']
        for alias in ['gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna']:
            op=result[alias]['operations'][0]
            self.assertEqual(op['endpoint'],'/v1/chat/completions')
            self.assertEqual(op['settings']['reasoning_effort']['enum'],['none','low','medium','high','xhigh','max'])
            self.assertEqual(op['settings']['max_completion_tokens']['max'],128000)
        self.assertEqual(result['gpt-5.6-sol-medium']['operations'],[])

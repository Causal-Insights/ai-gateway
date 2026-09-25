"""Discovery advertises finite executable audio contracts, never family-wide promises."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import advanced_audio
import capability_discovery as discovery
import voice_resources


class AudioDiscoveryTests(unittest.TestCase):
    def operations(self, model):
        return {item['id']: item for item in discovery.audio_contract(model)['operations']}

    def test_exact_speech_models_have_distinct_dialogue_and_finite_stream_scopes(self):
        v3, multi = self.operations('elevenlabs-v3-tts'), self.operations('elevenlabs-multilingual-v2')
        self.assertIn('speech.dialogue', v3)
        self.assertNotIn('speech.dialogue', multi)
        for operations, max_voices in ((v3, 10), (multi, 1)):
            self.assertEqual(operations['speech.generate']['response'], 'binary_audio')
            self.assertTrue(operations['speech.generate']['optional_timestamp_flag']['with_timestamps'])
            stream = operations['speech.incremental']
            self.assertEqual(stream['execution_modes'], ['websocket'])
            self.assertEqual(stream['settings']['voices']['max_unique'], max_voices)
            self.assertTrue(stream['ticket']['single_use'])
            self.assertTrue(stream['final_artifact_retained'])
            self.assertEqual(stream['input_messages'], ['text', 'flush', 'close'])
            self.assertEqual(stream['settings']['response_format']['aliases']['wav'], advanced_audio.output_format({'response_format': 'wav'}))

    def test_music_versions_keep_distinct_plans_import_and_editing(self):
        old, new = self.operations('elevenlabs-music'), self.operations('elevenlabs-music-2.5')
        self.assertEqual(old['audio.generate']['settings']['composition_plan']['shape'], 'sections')
        self.assertEqual(new['audio.generate']['settings']['composition_plan']['shape'], 'chunks')
        self.assertNotIn('music.import', old)
        self.assertNotIn('music.edit', old)
        for mode in ('music.import', 'music.edit', 'music.extend'):
            self.assertIn(mode, new)
            self.assertTrue(new[mode]['owner_delegation_required'])
        self.assertFalse(new['music.import']['billing']['free'])
        self.assertEqual(new['music.plan']['billing']['zero_reason'], 'provider_documented_free')
        self.assertEqual(new['music.edit']['settings']['composition_plan']['song_ids'], 'owned_gateway_ids')
        self.assertEqual(new['audio.generate']['settings']['seed']['min'], 0)
        self.assertEqual(new['audio.generate']['settings']['output_format']['default'], 'auto')
        self.assertFalse(discovery.audio_contract('elevenlabs-music-2.5')['resources']['songs']['provider_cleanup_supported'])

    def test_sfx_and_private_voice_resources_are_advertised_only_on_exact_implementations(self):
        sfx = self.operations('elevenlabs-sfx')['audio.generate']
        self.assertEqual(sfx['settings']['duration_seconds'], {'type': 'number', 'min': .5, 'max': 30})
        self.assertIn('loop', sfx['settings'])
        self.assertNotIn('voices', discovery.audio_contract('elevenlabs-sfx')['resources'])
        voices = discovery.audio_contract('elevenlabs-v3-tts')['resources']['voices']
        self.assertEqual(voices['creation_kind'], 'instant_voice_clone')
        self.assertEqual(voices['sample_limits']['max_total_bytes'], voice_resources.MAX_SAMPLE_BYTES)
        self.assertFalse(voices['sample_bytes_retained'])
        self.assertEqual(voices['verification'], 'provider_status_refreshed_by_get')
        self.assertIsNone(discovery.audio_contract('elevenlabs-unimplemented-model'))

    def test_advertised_custom_routes_exist_and_model_filter_hash_is_stable(self):
        routes = {route.path for router in (advanced_audio.router, voice_resources.router) for route in router.routes}
        for model in discovery.AUDIO_MODELS:
            for operation in self.operations(model).values():
                if operation['id'] != 'speech.generate':
                    self.assertIn(operation['endpoint'], routes)
        models = {alias: {'upstream_model': upstream} for alias, upstream in {**advanced_audio.SPEECH, **advanced_audio.MUSIC, 'elevenlabs-sfx': 'eleven_text_to_sound_v2'}.items()}
        models['elevenlabs-future'] = {'upstream_model': 'unknown'}
        registry = SimpleNamespace(models=models, document={})
        with patch.object(discovery, 'registry', return_value=registry), patch.object(discovery, 'discovery_contract', return_value=None):
            document = discovery.document(['elevenlabs-v3-tts'])
            self.assertEqual(set(document['models']), {'elevenlabs-v3-tts'})
            self.assertEqual(document['version'], discovery.document(['elevenlabs-v3-tts'])['version'])
            self.assertNotEqual(document['version'], discovery.document()['version'])
            self.assertEqual(discovery.document()['models']['elevenlabs-future']['operations'], [])
            models['elevenlabs-v3-tts']['disabled'] = True
            self.assertEqual(discovery.document(['elevenlabs-v3-tts'])['models'], {})

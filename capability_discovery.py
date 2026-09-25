"""Versioned discovery for implemented Gateway operations, not provider promises."""
import hashlib
import json

from fastapi import APIRouter, Depends
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from pricing_registry import registry
from video_capabilities import MODELS as VIDEO_MODELS, REVISION, profiles, settings_for
from hosted_tools import discovery_contract

router = APIRouter(tags=['capabilities'])


def video_contract(model):
    operations = []
    for profile in profiles(model):
        topology = profile.split('.', 1)[1]
        roles = {'text': [], 'first_frame': ['first_frame'], 'last_frame': ['last_frame'],
                 'first_last_frames': ['first_frame', 'last_frame'], 'references': ['reference'],
                 'first_frame_references': ['first_frame', 'reference'],
                 'multimodal_references': ['reference', 'reference_video', 'reference_audio'],
                 'source_video': ['source', 'reference'], 'keyframes': ['keyframe', 'reference'],
                 'previous_job': [], 'draft': ['reference', 'reference_video', 'reference_audio'], 'from_draft': []}[topology]
        if model.startswith('gemini-omni'):
            roles = [role for role in roles if role != 'reference_audio']
        operations.append({'id': profile, 'operation': profile.split('.')[0], 'media_roles': roles,
                           'settings': settings_for(model, profile), 'execution_modes': ['durable_job'],
                           'previous_job_required': topology in {'previous_job', 'from_draft'}})
    return {'contract_revision': REVISION, 'endpoint': '/v1/generation-jobs', 'request_schema_version': 2,
            'operations': operations, 'audio_mode': 'provider_managed' if model.startswith('gemini-omni') else 'selectable',
            'limitations': [] if model.startswith('veo-') else ['One video output per durable job.']}


AUDIO_MODELS = {'elevenlabs-v3-tts', 'elevenlabs-multilingual-v2', 'elevenlabs-sfx', 'elevenlabs-music', 'elevenlabs-music-2.5'}
AUDIO_REVISION = 'elevenlabs-advanced-audio-2026-09-24'


def audio_contract(model):
    """Finite Gateway contract; format availability still depends on the provider plan."""
    if model not in AUDIO_MODELS:
        return None
    speech = model in {'elevenlabs-v3-tts', 'elevenlabs-multilingual-v2'}
    music = model in {'elevenlabs-music', 'elevenlabs-music-2.5'}
    modern_music = model == 'elevenlabs-music-2.5'
    formats = {'type': 'string', 'default': 'auto' if music else 'mp3_44100_128',
               'aliases': {'mp3': 'mp3_44100_128', 'wav': 'pcm_44100', 'pcm': 'pcm_24000', 'opus': 'opus_48000_128'},
               'examples': ['mp3_44100_128', 'pcm_24000', 'pcm_44100', 'opus_48000_128'],
               'availability': 'provider_model_and_subscription', 'pcm_delivery_container': 'wav'}
    result = {'contract_revision': AUDIO_REVISION, 'operations': [],
              'owner_auth': {'header': 'X-Gateway-Resource-Owner', 'trusted_key_metadata': ['gateway_app_id', 'gateway_resource_delegate'],
                             'scope': 'application_and_owner'},
              'recovery': {'endpoint': '/v1/audio/requests/{request_id}', 'idempotency_header': 'Idempotency-Key',
                           'ambiguous_submission_retry': False},
              'resources': {}, 'limitations': ['Provider format and voice availability depend on the authenticated ElevenLabs account.']}
    def operation(identifier, endpoint, settings=None, **extra):
        return {'id': identifier, 'endpoint': endpoint, 'method': 'POST', 'execution_modes': ['sync'],
                'owner_delegation_required': True, 'response': 'json_audio_base64_and_metadata',
                'settings': settings or {}, **extra}
    if speech:
        settings = {'input': {'type': 'string', 'required': True}, 'voice': {'type': 'string', 'required': True, 'private_resource_syntax': 'voice:voice_<id>'},
                    'response_format': formats}
        result['operations'] = [
            operation('speech.generate', '/v1/audio/speech', settings, owner_delegation_required='private_voice_only', response='binary_audio',
                      optional_timestamp_flag={'with_timestamps': True, 'response': 'json_audio_base64_and_alignment', 'owner_delegation_required': True}),
            operation('speech.timestamps', '/v1/audio/speech-with-timestamps', {**settings, 'voice_settings': {'type': 'object'},
                      'language_code': {'type': 'string'}, 'seed': {'type': 'integer'}, 'previous_text': {'type': 'string'}, 'next_text': {'type': 'string'}}),
            operation('speech.incremental', '/v1/audio/sessions', {'voices': {'type': 'array', 'max_unique': 10 if model == 'elevenlabs-v3-tts' else 1},
                      'max_characters': {'type': 'integer', 'required': True, 'min': 1, 'max': 2147483647}, 'response_format': formats},
                      execution_modes=['websocket'], response='scoped_session_ticket', stream_endpoint='/v1/audio/sessions/{id}/stream',
                      ticket={'query_parameter': 'session_token', 'single_use': True, 'expires_seconds': 60},
                      input_messages=['text', 'flush', 'close'], output_messages=['audio', 'completed', 'error'],
                      max_session_seconds=300, final_artifact_retained=True)
        ]
        if model == 'elevenlabs-v3-tts':
            result['operations'].append(operation('speech.dialogue', '/v1/audio/dialogue',
                {'inputs': {'type': 'array', 'min_items': 1, 'max_unique_voices': 10, 'ordered': True, 'item_fields': ['text', 'voice']},
                 'with_timestamps': {'type': 'boolean', 'default': False}, 'response_format': formats}))
        result['resources']['voices'] = {'endpoint': '/v1/voices', 'item_endpoint': '/v1/voices/{id}',
            'operations': ['create', 'list', 'get_refresh', 'delete'], 'creation_kind': 'instant_voice_clone',
            'multipart_fields': ['name', 'request_id', 'consent', 'files'], 'sample_limits': {'max_files': 10, 'max_total_bytes': 25 * 1024 * 1024},
            'consent_required_fields': ['version', 'actor_id', 'accepted_at'], 'verification': 'provider_status_refreshed_by_get',
            'sample_bytes_retained': False, 'id_namespace': 'voice_', 'owner_only': True}
    else:
        settings = {'prompt': {'type': 'string'}, 'output_format': formats}
        if not music:
            settings.update({'duration_seconds': {'type': 'number', 'min': .5, 'max': 30},
                             'prompt_influence': {'type': 'number', 'min': 0, 'max': 1}, 'loop': {'type': 'boolean'}})
            result['operations'] = [operation('audio.generate', '/v1/audio/generations', settings)]
        else:
            plan = {'type': 'object', 'shape': 'chunks' if modern_music else 'sections', 'generated_duration_ms': {'min': 3000, 'max': 120000},
                    'generated_fields': ['text', 'positive_styles', 'negative_styles', 'duration_ms', 'conditioning_ref', 'condition_strength', 'context_adherence'] if modern_music else
                        ['section_name', 'positive_local_styles', 'negative_local_styles', 'duration_ms', 'lines']}
            if modern_music:
                plan.update({'max_chunks': 30, 'reference_fields': ['song_id', 'range.start_ms', 'range.end_ms'],
                             'song_ids': 'owned_gateway_ids', 'min_range_ms': 50, 'max_conditioning_range_ms': 30000,
                             'condition_strength': ['low', 'medium', 'high', 'xhigh'], 'context_adherence': ['low', 'medium', 'high']})
            settings.update({'composition_plan': plan, 'seed': {'type': 'integer', 'min': 0, 'max': 2147483647, 'requires': 'composition_plan'},
                'music_length_ms': {'type': 'integer', 'min': 3000, 'max': 600000, 'requires': 'prompt'},
                'force_instrumental': {'type': 'boolean', 'requires': 'prompt'}, 'prompt': {'type': 'string', 'max_length': 4100}})
            settings['store_for_inpainting' if modern_music else 'respect_sections_durations'] = {'type': 'boolean', 'default': True}
            result['operations'] = [operation('audio.generate', '/v1/audio/generations', settings, exclusive_inputs=['prompt', 'composition_plan']),
                operation('music.plan', '/v1/audio/music/plan', {'prompt': settings['prompt'], 'music_length_ms': settings['music_length_ms'], 'source_composition_plan': plan},
                          response='composition_plan', billing={'zero_reason': 'provider_documented_free'})]
            result['resources']['songs'] = {'endpoint': '/v1/songs', 'item_endpoint': '/v1/songs/{id}', 'content_endpoint': '/v1/songs/{id}/content',
                'operations': ['list', 'get', 'download', 'delete'], 'id_namespace': 'song_', 'owner_only': True, 'provider_cleanup_supported': False}
            if modern_music:
                result['operations'].append(operation('music.import', '/v1/songs', {'file': {'type': 'binary', 'required': True}, 'request_id': {'type': 'string'}},
                    content_type='multipart/form-data', response='owned_song_and_audio', billing={'unit': 'audio_seconds', 'free': False},
                    upload_limit_configuration='GENERATION_MAX_UPLOAD_BYTES'))
                for action in ('edit', 'extend'):
                    result['operations'].append(operation('music.' + action, '/v1/songs/{song_id}/takes',
                        {'operation': {'values': [action]}, 'composition_plan': plan, 'chunks': {'alias_for': 'composition_plan.chunks'},
                         'seed': settings['seed'], 'output_format': formats}, source_song_reference_required=True))
        result['legacy_endpoint'] = '/v1/images/generations'
    return result


def image_contract(model):
    from openai_model_contracts import IMAGE_MODELS, IMAGE_QUALITIES, IMAGE_PRESERVED_FIELDS
    from gateway_request_policy import GROK_IMAGE_2_ASPECT_RATIOS, GROK_IMAGE_2_QUALITIES
    settings, modes, extra = {}, ['sync'], {}
    edit_endpoint = '/v1/images/edits'
    roles = ['reference', 'mask']
    if model in {'gpt-image-1.5', 'gpt-image-2'} | set(IMAGE_MODELS):
        modern = model in IMAGE_MODELS
        settings = {'quality': {'values': sorted(IMAGE_QUALITIES) if modern else ['auto', 'low', 'medium', 'high']},
                    'output_format': {'values': ['png', 'jpeg', 'webp']}, 'output_compression': {'min': 0, 'max': 100, 'formats': ['jpeg', 'webp']},
                    'background': {'values': ['auto', 'opaque', 'transparent'], 'transparent_formats': ['png', 'webp']},
                    'moderation': {'values': ['auto', 'low']}, 'partial_images': {'min': 0, 'max': 3}}
        if modern:
            settings.update(size={'default': 'auto', 'format': 'WIDTHxHEIGHT', 'dimension_multiple': 16, 'max_dimension': 3840,
                                  'min_pixels': 655360, 'max_pixels': 8294400, 'max_aspect_ratio': 3}, n={'min': 1, 'max': 4})
            extra['preserved_fields'] = list(IMAGE_PRESERVED_FIELDS)
        if model == 'gpt-image-2.5-flare':
            extra['streaming'] = {'adapter_implemented': True, 'availability': 'unverified_provider_documentation_conflict'}
            settings.pop('partial_images')
        else:
            modes.append('stream')
            extra['streaming'] = {'delivery': 'native_sse', 'partial_images_are_final': False, 'usage_source': 'completed_events'}
    elif model in {'seedream-5.0', 'seedream-5.0-lite', 'seedream-5.0-pro'}:
        pro = model == 'seedream-5.0-pro'
        edit_endpoint, roles = '/v1/images/generations', ['reference']
        settings = {'size': {'presets': ['1K', '1.5K', '2K'] if pro else ['2K', '3K', '4K'], 'custom_dimensions': True},
                    'output_format': {'values': ['png', 'jpeg']}, 'response_format': {'values': ['url', 'b64_json']},
                    'watermark': {'type': 'boolean'}, 'seed': {'type': 'integer'},
                    'optimize_prompt_options': {'fields': {'mode': ['standard', 'fast'] if pro else ['standard']}}}
        extra['all_outputs_retained'] = True
        if pro:
            settings['size'].update(min_pixels=921600, max_pixels=4624220, max_aspect_ratio=16)
            settings.update(n={'values': [1]}, background={'values': ['opaque', 'transparent'], 'transparent_requires': 'one_alpha_reference_and_png'})
            extra.update(max_reference_images=10, streaming={'supported': False}, sequential_generation=False)
        else:
            settings.update(sequential_image_generation={'values': ['disabled', 'auto']}, sequential_image_generation_options={'max_images': {'min': 1, 'max': 15}}, stream={'type': 'boolean'})
            modes.append('buffered_stream')
            extra.update(streaming={'delivery': 'buffered_complete_response', 'upstream_event_transport': 'sse', 'live_partial_delivery': False},
                         combined_reference_and_output_limit=15)
    elif model in {'grok-image', 'grok-imagine-image-quality', 'grok-imagine-image-2.0'}:
        edit_endpoint, roles = '/v1/images/generations', ['reference']
        settings = {'resolution': {'values': ['1k', '2k']}, 'response_format': {'values': ['url', 'b64_json']}}
        if model == 'grok-imagine-image-2.0':
            settings.update(quality={'values': sorted(GROK_IMAGE_2_QUALITIES), 'default': 'medium'},
                            aspect_ratio={'values': sorted(GROK_IMAGE_2_ASPECT_RATIOS)},
                            size={'aliases': 'resolution_or_aspect_ratio_or_equivalent_dimensions'})
    elif model in {'nano-banana', 'nano-banana-2', 'nano-banana-2-lite', 'nano-banana-pro'}:
        video = model != 'nano-banana-pro'
        roles = ['reference'] + (['reference_video'] if video else [])
        operation = {'id': 'image.generate', 'endpoint': '/v1/chat/completions', 'execution_modes': ['sync'],
                     'media_roles': roles, 'settings': {'modalities': {'values': [['text', 'image']]}, 'extra_body.generationConfig.imageConfig': {'fields': ['aspectRatio'] if model == 'nano-banana-2-lite' else ['aspectRatio', 'imageSize']}}}
        return {'contract_revision': 'image-wire-v2-2026-09-24', 'operations': [operation, {**operation, 'id': 'image.edit'}],
                'reference_video': {'supported': video, **({'wire': {'type': 'file', 'file': {'file_id': 'HTTPS_URL', 'format': 'video/mp4'}}} if video else {})}}
    else:
        return None
    operations = [{'id': 'image.generate', 'endpoint': '/v1/images/generations', 'execution_modes': modes, 'settings': settings},
                  {'id': 'image.edit', 'endpoint': edit_endpoint, 'execution_modes': modes, 'media_roles': roles, 'settings': settings}]
    if model == 'seedream-5.0-pro':
        operations.append({'id': 'image.layers', 'endpoint': '/v1/images/generations', 'execution_modes': ['sync'],
                           'media_roles': ['reference'], 'reference_count': 1, 'prompt_optional': True,
                           'settings': {**settings, 'layer_decomposition': {'values': [True]}, 'size': {'presets': ['auto', '1K', '1.5K', '2K']}, 'output_format': {'values': ['png']}, 'n': {'values': [1]}},
                           'all_layers_retained': True, 'layer_metadata': ['image_index', 'z_index', 'bounding_box', 'name', 'description']})
    return {'contract_revision': 'image-wire-v2-2026-09-24', 'operations': operations, **extra}


def add_native_vertex_contract(item, model, upstream):
    from video_capabilities import OMNI
    if model in OMNI:
        item['operations'].append({'id': 'text.interaction', 'endpoint': '/v1/interactions', 'method': 'POST',
            'execution_modes': ['sync', 'stream', 'background'], 'response_format': {'type': 'text'},
            'media_roles': ['text', 'image', 'video'], 'ordered_inputs': True, 'owner_delegation_required': True,
            'settings': {'previous_interaction_id': {'type': 'owned_interaction_id'}, 'store': {'type': 'boolean', 'default': True},
                         'generation_config': {'fields': ['temperature', 'top_p', 'seed', 'stop_sequences', 'thinking_level', 'max_output_tokens']}},
            'background_requires_store': True, 'streaming': {'delivery': 'native_sse', 'final_artifact_retained': True,
                'interrupted_retrieval_requires_store': True},
            'terminal_statuses': ['completed', 'failed', 'cancelled', 'incomplete']})
        item.setdefault('resources', {})['interactions'] = {'item_endpoint': '/v1/interactions/{interaction_id}', 'operations': ['get', 'delete'], 'owner_only': True}
    if upstream.startswith('vertex_ai/gemini-'):
        item['operations'].append({'id': 'tokens.count', 'endpoint': '/v1/models/{model}/count-tokens', 'method': 'POST',
            'execution_modes': ['sync'], 'request_fields': ['contents', 'systemInstruction', 'tools', 'generationConfig'],
            'media_sources': ['https', 'inline_data']})


def document(allowed_models=None):
    models = {}
    for alias, entry in registry().models.items():
        if entry.get('disabled') or (allowed_models and not {'*', 'all-proxy-models'}.intersection(allowed_models) and alias not in allowed_models):
            continue
        item = {'upstream_model': entry['upstream_model'], 'operations': []}
        if alias in VIDEO_MODELS:
            item.update(video_contract(alias))
        elif image_contract(alias) is not None:
            item.update(image_contract(alias))
        elif alias in AUDIO_MODELS:
            item.update(audio_contract(alias))
        else:
            # Existing model discovery remains authoritative for general text and
            # native SDK routes; no unverified hosted-tool support is inferred.
            item['discovery_scope'] = 'existing_model_registry'
        if alias in {'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'}:
            item['operations'].append({'id': 'text.generate', 'endpoint': '/v1/chat/completions',
                'execution_modes': ['sync', 'stream'], 'settings': {
                    'reasoning_effort': {'type': 'string', 'enum': ['none', 'low', 'medium', 'high', 'xhigh', 'max'], 'default': 'medium'},
                    'max_completion_tokens': {'type': 'integer', 'min': 1, 'max': 128000}},
                'fixed_effort_aliases_unchanged': True})
        add_native_vertex_contract(item, alias, entry['upstream_model'])
        tariffs = [registry().profiles[version].get('grounding_tariff') for version in entry.get('profiles', [])
                   if registry().profiles[version].get('enabled')]
        tariff = next((value for value in tariffs if value), None)
        if tariff:
            google = entry['upstream_model'].startswith('vertex_ai/')
            item['operations'].append({'id': 'generation.grounded',
                'endpoint': '/v1/chat/completions' if google else '/v1/responses',
                'execution_modes': ['sync', 'stream'],
                'tools': [{'google_search': {}}] if google else [{'type': 'web_search'}, {'type': 'x_search'}],
                'pricing': tariff, 'usage_source': 'sdk_query_count_estimate' if google else 'provider_usage_counts',
                'cost_status_when_usage_incomplete': 'unresolved', 'outputs_retained': True})
        hosted = discovery_contract(alias, registry().document)
        if hosted:
            item.update(hosted)
        models[alias] = item
    canonical = json.dumps(models, sort_keys=True, separators=(',', ':'))
    return {'version': 'gateway-capabilities-v1-' + hashlib.sha256(canonical.encode()).hexdigest()[:16],
            'schema_version': 1, 'models': models, 'retained_snapshot_fallback': True}


@router.get('/v1/capabilities')
async def get_capabilities(user: UserAPIKeyAuth = Depends(user_api_key_auth)):
    return document(user.models)

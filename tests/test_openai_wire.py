"""Offline end-to-end LiteLLM dispatch goldens; run in the pinned image."""
import json
import unittest
from unittest.mock import patch
from test_litellm_compatibility import _litellm_is_installed


@unittest.skipUnless(_litellm_is_installed(), 'requires pinned application image')
class OpenAIWireTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # These tests isolate provider wire contracts. Gateway admission and
        # persistence are exercised with authenticated intents in runtime tests.
        import litellm
        self.callbacks = patch.object(litellm, "callbacks", [])
        self.callbacks.start()
        self.addCleanup(self.callbacks.stop)

    async def asyncSetUp(self):
        from openai_model_contracts import install_image_adapters
        from openai_usage import install_openai_accounting
        install_image_adapters()
        install_openai_accounting()

    async def test_generation_and_edit_preserve_exact_controls_usage_and_formats(self):
        import httpx
        import litellm
        from openai_model_contracts import IMAGE_MODELS
        from gateway_request_policy import apply_request_policy
        usage = dict(input_tokens=3, output_tokens=10, total_tokens=13,
                     input_tokens_details=dict(text_tokens=1, image_tokens=2))
        for model in IMAGE_MODELS.values():
            for operation in ('aimage_generation', 'aimage_edit'):
                seen = []
                async def post(client, request, **kwargs):
                    raw = await request.aread()
                    if "multipart/form-data" in request.headers.get("content-type", ""):
                        from email.parser import BytesParser
                        from email.policy import default
                        envelope = BytesParser(policy=default).parsebytes(b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + raw)
                        fields, files = {}, []
                        for part in envelope.iter_parts():
                            name = part.get_param("name", header="content-disposition")
                            if part.get_filename(): files.append(name)
                            else: fields[name] = part.get_payload(decode=True).decode()
                        seen.append((str(request.url), dict(data=fields, files=files)))
                    else:
                        seen.append((str(request.url), dict(data=raw)))
                    return httpx.Response(200, json=dict(created=1, data=[dict(b64_json='aW1hZ2U=')],
                        usage=usage, output_format='webp'), request=request)
                args = dict(model='openai/' + model, prompt='A small illustration', api_key='offline-test',
                    quality='max', size='1536x1024', output_format='webp', output_compression=80,
                    background='transparent', n=1, num_retries=0)
                if operation == 'aimage_edit':
                    args.update(image=[b'base', b'ref'], mask=b'mask')
                else:
                    args, error = apply_request_policy('/v1/images/generations', args)
                    self.assertIsNone(error)
                with patch('httpx.AsyncClient.send', new=post):
                    result = await getattr(litellm, operation)(**args)
                self.assertEqual(len(seen), 1)
                url, request = seen[0]
                payload = request.get('json') or request.get('data')
                if isinstance(payload, (str, bytes)): payload = json.loads(payload)
                for field in ('quality', 'size', 'output_format', 'output_compression', 'background', 'n'):
                    self.assertIn(field, payload, (operation, payload))
                    self.assertEqual(str(payload[field]), str(args[field]), (operation, field))
                self.assertEqual(payload['model'], model)
                self.assertEqual(result.output_format, 'webp')
                self.assertEqual(result.usage.input_tokens, 3)
                self.assertEqual(result.usage.output_tokens, 10)
                if operation == 'aimage_edit':
                    self.assertTrue(request.get('files'))

    async def test_astra_responses_tools_reasoning_and_usage_survive_dispatch(self):
        import httpx
        import litellm
        from gateway_request_policy import apply_request_policy
        seen = []
        response = dict(id='resp_offline', object='response', created_at=1, status='completed',
            model='gpt-6-astra', parallel_tool_calls=False, tool_choice='auto', tools=[],
            output=[dict(type='image_generation_call', id='ig_offline', status='completed', result='aW1hZ2U=',
                usage=dict(input_tokens=1, output_tokens=1, total_tokens=2, input_tokens_details=dict(text_tokens=1, image_tokens=0)))],
            usage=dict(input_tokens=100, output_tokens=20, total_tokens=120,
                input_tokens_details=dict(cached_tokens=0, cache_write_tokens=50)))
        async def post(client, request, **kwargs):
            seen.append((str(request.url), dict(data=await request.aread())))
            return httpx.Response(200, json=response, request=request)
        body, error = apply_request_policy('/v1/responses', dict(model='gpt-6-astra', input='Draw a square',
            reasoning=dict(effort='max'), prompt_cache_options=dict(mode='explicit', ttl='30m'), max_output_tokens=100, max_tool_calls=1, parallel_tool_calls=False,
            tools=[dict(type='image_generation', model='gpt-image-2.5-flare', quality='xhigh', output_format='webp')]))
        self.assertIsNone(error)
        body['model'] = 'openai/gpt-6-astra'
        with patch('httpx.AsyncClient.send', new=post):
            result = await litellm.aresponses(**body, api_key='offline-test', num_retries=0)
        self.assertEqual(len(seen), 1)
        payload = seen[0][1].get('json') or seen[0][1].get('data')
        if isinstance(payload, (str, bytes)): payload = json.loads(payload)
        self.assertEqual(payload['reasoning']['effort'], 'max')
        self.assertEqual(payload['max_tool_calls'], 1)
        self.assertEqual(payload['prompt_cache_options'], dict(mode='explicit', ttl='30m'))
        self.assertEqual(payload['tools'][0]['model'], 'gpt-image-2.5-flare-2026-09-08')
        self.assertEqual(payload['tools'][0]['quality'], 'xhigh')
        self.assertEqual(result.usage.model_dump()['input_tokens_details']['cache_write_tokens'], 50)
        self.assertIn('usage', result.output[0].model_dump())

    async def test_astra_chat_reasoning_structured_output_and_provider_failure(self):
        import httpx
        import litellm
        from gateway_request_policy import apply_request_policy
        seen = []
        async def post(client, request, **kwargs):
            seen.append(json.loads(await request.aread()))
            return httpx.Response(404, json=dict(error=dict(message='Model unavailable for this project.', type='invalid_request_error', code='model_not_found')), request=request)
        schema = dict(type='json_schema', json_schema=dict(name='result', strict=True,
            schema=dict(type='object', properties=dict(ok=dict(type='boolean')), required=['ok'], additionalProperties=False)))
        body, error = apply_request_policy('/v1/chat/completions', dict(model='openai/gpt-6-astra',
            messages=[dict(role='user', content='Return ok true')], reasoning_effort='max',
            max_completion_tokens=100, response_format=schema))
        self.assertIsNone(error)
        with patch('httpx.AsyncClient.send', new=post):
            with self.assertRaises(litellm.NotFoundError):
                await litellm.acompletion(**body, api_key='offline-test', num_retries=0, max_retries=0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['reasoning_effort'], 'max')
        self.assertEqual(seen[0]['response_format'], schema)
        self.assertEqual(seen[0]['max_completion_tokens'], 100)

    async def test_image_transient_failure_is_not_resubmitted(self):
        import httpx
        import litellm
        from gateway_request_policy import apply_request_policy
        attempts = []
        async def post(client, request, **kwargs):
            attempts.append(str(request.url))
            return httpx.Response(503, json=dict(error=dict(message='Provider unavailable.', type='server_error')), request=request)
        body, error = apply_request_policy('/v1/images/generations', dict(model='openai/gpt-image-2.5-flare-2026-09-08', prompt='Offline test'))
        self.assertIsNone(error)
        with patch('httpx.AsyncClient.send', new=post):
            with self.assertRaises(litellm.ServiceUnavailableError):
                await litellm.aimage_generation(**body, api_key='offline-test', num_retries=0, max_retries=0)
        self.assertEqual(len(attempts), 1)

    async def test_astra_stream_preserves_terminal_usage(self):
        import httpx
        import litellm
        usage = dict(input_tokens=10, output_tokens=2, total_tokens=12,
                     input_tokens_details=dict(cached_tokens=0, cache_write_tokens=0))
        response = dict(id='resp_stream', object='response', created_at=1, status='completed', model='gpt-6-astra',
            parallel_tool_calls=False, tool_choice='auto', tools=[], output=[], usage=usage)
        event = dict(type='response.completed', sequence_number=0, response=response)
        async def send(client, request, **kwargs):
            return httpx.Response(200, headers={'content-type':'text/event-stream'},
                content=('event: response.completed\ndata: ' + json.dumps(event) + '\n\n').encode(), request=request)
        with patch('httpx.AsyncClient.send', new=send):
            stream = await litellm.aresponses(model='openai/gpt-6-astra', input='Hello', stream=True,
                                            api_key='offline-test', max_retries=0, num_retries=0)
            events = [event async for event in stream]
        final = next(event for event in events if event.type == 'response.completed')
        self.assertEqual(final.response.usage.input_tokens, 10)
        self.assertEqual(final.response.usage.model_dump()['input_tokens_details']['cache_write_tokens'], 0)

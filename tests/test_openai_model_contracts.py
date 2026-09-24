import unittest
from gateway_request_policy import apply_request_policy
from openai_model_contracts import IMAGE_MODELS, validate_astra, validate_image_settings


class OpenAIContractsTests(unittest.TestCase):
    def test_image_matrix(self):
        for model in IMAGE_MODELS:
            for quality in ("auto", "low", "medium", "high", "xhigh", "max"):
                for fmt in ("png", "jpeg", "webp"):
                    body = dict(model=model, quality=quality, output_format=fmt, size="1536x1024", n=4)
                    self.assertIsNone(apply_request_policy("/v1/images/generations", body)[1])

    def test_invalid_images(self):
        cases = [dict(quality="ultra"), dict(size="1025x1024"), dict(size="3840x3840"),
                 dict(size="4096x1024"), dict(size="1024x256"), dict(size="1024x512"),
                 dict(output_format="jpeg", background="transparent"), dict(output_compression=80),
                 dict(output_format="webp", output_compression=True), dict(n=5), dict(n=True),
                 dict(partial_images=4), dict(input_fidelity="high")]
        for settings in cases:
            with self.subTest(settings=settings):
                self.assertIsNotNone(validate_image_settings(settings))

    def test_astra_efforts_and_nested_tool(self):
        for effort in ("low", "medium", "high", "xhigh", "max"):
            body = dict(model="gpt-6-astra", reasoning=dict(effort=effort), tools=[
                dict(type="image_generation", model="gpt-image-2.5-flare", quality="max")])
            mapped, error = apply_request_policy("/v1/responses", body)
            self.assertIsNone(error)
            self.assertEqual(mapped["tools"][0]["model"], IMAGE_MODELS["gpt-image-2.5-flare"])
        for setting in [dict(temperature=0.7), dict(top_p=1), dict(reasoning=dict(effort="none")),
                        dict(reasoning="high"), dict(tools=[None])]:
            self.assertIsNotNone(validate_astra(setting, responses=True))

    def test_model_scope_and_image_route(self):
        body = dict(model="gpt-5.5", temperature=.7)
        self.assertEqual(apply_request_policy("/v1/responses", body), (body, None))
        body = dict(model="gpt-6-astra", tools=[dict(type="image_generation")])
        self.assertIsNotNone(apply_request_policy("/v1/chat/completions", body)[1])
        for path in ("/v1/images/generations", "/v1/images/edits"):
            body = dict(model="gpt-image-2.5-flare", stream=True)
            self.assertEqual(apply_request_policy(path, body)[1].code, "IMAGE_STREAMING_UNAVAILABLE")
        body = dict(model="gpt-6-astra", extra_body=dict(temperature=.4))
        self.assertIsNotNone(apply_request_policy("/v1/responses", body)[1])



if __name__ == "__main__":
    unittest.main()

class OpenAIAccountingTests(unittest.TestCase):
    def test_astra_cache_partition_and_long_context_boundary(self):
        from openai_usage import astra_cost
        usage = dict(input_tokens=272000, output_tokens=100,
                     input_tokens_details=dict(cached_tokens=1000, cache_write_tokens=2000),
                     output_tokens_details=dict(reasoning_tokens=50))
        expected = (269000 * 10 + 1000 + 2000 * 12.5 + 100 * 50) / 1000000
        self.assertAlmostEqual(astra_cost(usage), expected)
        usage['input_tokens'] += 1
        expected = ((269001 * 10 + 1000 + 2000 * 12.5) * 2 + 100 * 75) / 1000000
        self.assertAlmostEqual(astra_cost(usage), expected)
        self.assertAlmostEqual(astra_cost(usage, 'priority'), expected * 2)
        self.assertAlmostEqual(astra_cost(dict(input_tokens=3, output_tokens=1)), 0.00008)

    def test_image_tokens_and_cached_modalities(self):
        from openai_usage import image_cost, response_cost
        usage = dict(input_tokens=300, output_tokens=1000, input_tokens_details=dict(
            text_tokens=100, image_tokens=200, cached_tokens=30,
            cached_tokens_details=dict(text_tokens=10, image_tokens=20)))
        cost = (90 * 5 + 10 * 1.25 + 180 * 8 + 20 * 2 + 1000 * 30) / 1000000
        self.assertAlmostEqual(image_cost(usage), cost)
        parent = dict(input_tokens=100, output_tokens=50, input_tokens_details=dict(cached_tokens=0, cache_write_tokens=0))
        response = dict(usage=parent, output=[dict(type='image_generation_call', usage=usage)])
        params = dict(tools=[dict(type='image_generation', model='gpt-image-2.5-flare')])
        self.assertIsNone(response_cost('gpt-6-astra', response, params))
        del response['output'][0]['usage']
        self.assertIsNone(response_cost('gpt-6-astra', response, params))

    def test_missing_and_malformed_usage_never_becomes_free(self):
        from openai_usage import image_cost
        self.assertIsNone(image_cost(None))
        self.assertIsNone(image_cost(dict(input_tokens=2, output_tokens=-1, input_tokens_details=dict(text_tokens=2, image_tokens=0))))


class OpenAIMultipartTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, fields):
        from gateway_request_policy import GatewayRequestPolicyMiddleware
        boundary = 'contract-boundary'
        raw = b''
        for name, value in fields.items():
            raw += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        raw += f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="input.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + b'\x89PNG\r\n\x00\xff' + f'\r\n--{boundary}--\r\n'.encode()
        seen, sent = [], []
        async def app(scope, receive, send):
            seen.append((await receive())['body'])
        async def receive():
            return dict(type='http.request', body=raw, more_body=False)
        async def send(message):
            sent.append(message)
        await GatewayRequestPolicyMiddleware(app)(dict(type='http', method='POST', path='/v1/images/edits', headers=[(b'content-type', f'multipart/form-data; boundary={boundary}'.encode())]), receive, send)
        return raw, seen, sent

    async def test_valid_edit_keeps_bytes_and_controls(self):
        raw, seen, sent = await self.invoke(dict(model='gpt-image-2.5-flare', quality='max', output_format='webp', output_compression=80, n=4))
        self.assertEqual(seen, [raw])
        self.assertEqual(sent, [])

    async def test_invalid_edit_never_dispatches(self):
        raw, seen, sent = await self.invoke(dict(model='gpt-image-2.5-flare', background='transparent', output_format='jpeg'))
        self.assertEqual(seen, [])
        self.assertEqual(sent[0]['status'], 400)

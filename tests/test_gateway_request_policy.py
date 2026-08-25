import json
import unittest

from gateway_request_policy import GatewayRequestPolicyMiddleware, apply_request_policy


class GatewayRequestPolicyTests(unittest.TestCase):
    def test_chat_alias_injects_fixed_reasoning_effort(self):
        body, error = apply_request_policy(
            "/v1/chat/completions",
            {"model": "gpt-5.6-luna-high", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertIsNone(error)
        self.assertEqual(body["reasoning_effort"], "high")

    def test_responses_alias_injects_nested_reasoning_effort(self):
        body, error = apply_request_policy(
            "/v1/responses", {"model": "gpt-5.6-sol-medium", "input": "hi"}
        )
        self.assertIsNone(error)
        self.assertEqual(body["reasoning"], {"effort": "medium"})

    def test_conflicting_reasoning_effort_is_rejected(self):
        _body, error = apply_request_policy(
            "/v1/responses",
            {"model": "gpt-5.6-luna-medium", "input": "hi", "reasoning": {"effort": "high"}},
        )
        self.assertIsNotNone(error)
        self.assertEqual(error.code, "FIXED_REASONING_EFFORT")

    def test_new_gemini_models_drop_unsupported_sampling_params(self):
        body, error = apply_request_policy(
            "/v1/chat/completions",
            {
                "model": "gemini-3.7-flash",
                "messages": [],
                "candidate_count": 2,
                "temperature": 0.2,
                "thinking_budget": 1024,
                "top_p": 0.9,
                "frequency_penalty": 0.5,
                "extra_body": {"topK": 10, "safe": True},
            },
        )
        self.assertIsNone(error)
        for field in (
            "candidate_count",
            "temperature",
            "thinking_budget",
            "top_p",
            "frequency_penalty",
        ):
            self.assertNotIn(field, body)
        self.assertEqual(body["extra_body"], {"safe": True})

    def test_omni_is_rejected_on_text_endpoints(self):
        for path in ("/v1/chat/completions", "/v1/responses"):
            _body, error = apply_request_policy(path, {"model": "gemini-omni-flash-preview"})
            self.assertIsNotNone(error)
            self.assertEqual(error.code, "OMNI_REQUIRES_DURABLE_JOB")

    def test_grok_image_2_preserves_fields_consumed_by_litellm(self):
        body, error = apply_request_policy(
            "/v1/images/generations",
            {
                "model": "grok-imagine-image-2.0",
                "prompt": "A poster with exact typography",
                "n": 2,
                "quality": "low",
                "resolution": "2K",
                "response_format": "b64_json",
            },
        )
        self.assertIsNone(error)
        self.assertEqual(body["xai_output_count"], 2)
        self.assertEqual(body["xai_render_quality"], "low")
        self.assertEqual(body["xai_response_format"], "b64_json")

    def test_grok_image_2_normalizes_openai_dimensions_for_xai(self):
        body, error = apply_request_policy(
            "/v1/images/generations",
            {
                "model": "grok-imagine-image-2.0",
                "prompt": "A square poster",
                "size": "1024x1024",
            },
        )
        self.assertIsNone(error)
        self.assertNotIn("size", body)
        self.assertEqual(body["aspect_ratio"], "1:1")
        self.assertEqual(body["resolution"], "1k")

    def test_grok_image_2_normalizes_2k_dimensions(self):
        body, error = apply_request_policy(
            "/v1/images/generations",
            {
                "model": "grok-imagine-image-2.0",
                "prompt": "A widescreen poster",
                "size": "2048x1152",
            },
        )
        self.assertIsNone(error)
        self.assertNotIn("size", body)
        self.assertEqual(body["aspect_ratio"], "16:9")
        self.assertEqual(body["resolution"], "2k")

    def test_grok_image_2_rejects_invalid_requests(self):
        cases = (
            ({"prompt": ""}, "INVALID_IMAGE_PROMPT"),
            ({"prompt": "ok", "quality": "high"}, "INVALID_IMAGE_QUALITY"),
            ({"prompt": "ok", "resolution": "4k"}, "INVALID_IMAGE_RESOLUTION"),
            ({"prompt": "ok", "size": "1234x1000"}, "INVALID_IMAGE_SIZE"),
            ({"prompt": "ok", "response_format": "jpeg"}, "INVALID_IMAGE_RESPONSE_FORMAT"),
            ({"prompt": "ok", "n": 0}, "INVALID_IMAGE_COUNT"),
        )
        for values, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                _body, error = apply_request_policy(
                    "/v1/images/generations",
                    {"model": "grok-imagine-image-2.0", **values},
                )
                self.assertIsNotNone(error)
                self.assertEqual(error.code, expected_code)

    def test_seedream_preserves_standard_image_fields_for_custom_dispatch(self):
        body, error = apply_request_policy(
            "/v1/images/generations",
            {
                "model": "seedream-5.0-pro",
                "prompt": "A wide editorial illustration",
                "size": "2816x1584",
                "n": 1,
                "response_format": "url",
                "output_format": "png",
            },
        )
        self.assertIsNone(error)
        self.assertEqual(body["seedream_size"], "2816x1584")
        self.assertEqual(body["seedream_output_count"], 1)
        self.assertEqual(body["seedream_response_format"], "url")
        self.assertEqual(body["seedream_output_format"], "png")
        self.assertEqual(body["size"], "2816x1584")

    def test_seedream_pro_rejects_invalid_requests_before_litellm_routing(self):
        cases = (
            ({"prompt": "", "size": "1K"}, "INVALID_IMAGE_PROMPT"),
            ({"prompt": "ok", "size": "4096x4096"}, "INVALID_IMAGE_SIZE"),
            ({"prompt": "ok", "n": 2}, "INVALID_IMAGE_COUNT"),
            ({"prompt": "ok", "output_format": "webp"}, "INVALID_IMAGE_OUTPUT_FORMAT"),
            ({"prompt": "ok", "stream": True}, "IMAGE_STREAMING_UNSUPPORTED"),
            (
                {"prompt": "ok", "sequential_image_generation": "auto"},
                "SEQUENTIAL_IMAGE_GENERATION_UNSUPPORTED",
            ),
            (
                {"prompt": "ok", "image_urls": [f"https://example.test/{i}.png" for i in range(11)]},
                "TOO_MANY_REFERENCE_IMAGES",
            ),
        )
        for values, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                _body, error = apply_request_policy(
                    "/v1/images/generations",
                    {"model": "seedream-5.0-pro", **values},
                )
                self.assertIsNotNone(error)
                self.assertEqual(error.code, expected_code)

    def test_seedance_2_5_rejects_legacy_images_route(self):
        _body, error = apply_request_policy(
            "/v1/images/generations",
            {"model": "seedance-2.5", "prompt": "A cinematic dolly shot"},
        )
        self.assertIsNotNone(error)
        self.assertEqual(error.code, "SEEDANCE_REQUIRES_DURABLE_JOB")


class GatewayRequestPolicyMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_disconnect_checks_reach_original_receive_channel(self):
        source_messages = [
            {
                "type": "http.request",
                "body": json.dumps(
                    {"model": "gpt-5.6-sol-medium", "messages": [{"role": "user", "content": "hi"}]}
                ).encode(),
                "more_body": False,
            },
            {"type": "http.disconnect", "reason": "real-client-disconnect"},
        ]
        received_by_app = []

        async def receive():
            return source_messages.pop(0)

        async def send(_message):
            return None

        async def app(_scope, replay, _send):
            received_by_app.append(await replay())
            received_by_app.append(await replay())

        middleware = GatewayRequestPolicyMiddleware(app)
        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "headers": [(b"content-type", b"application/json")],
            },
            receive,
            send,
        )

        replayed = json.loads(received_by_app[0]["body"])
        self.assertEqual(replayed["reasoning_effort"], "medium")
        self.assertEqual(
            received_by_app[1],
            {"type": "http.disconnect", "reason": "real-client-disconnect"},
        )


if __name__ == "__main__":
    unittest.main()

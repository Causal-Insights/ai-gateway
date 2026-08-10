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
                "model": "gemini-3.6-flash",
                "messages": [],
                "temperature": 0.2,
                "top_p": 0.9,
                "frequency_penalty": 0.5,
                "extra_body": {"topK": 10, "safe": True},
            },
        )
        self.assertIsNone(error)
        for field in ("temperature", "top_p", "frequency_penalty"):
            self.assertNotIn(field, body)
        self.assertEqual(body["extra_body"], {"safe": True})

    def test_omni_is_rejected_on_text_endpoints(self):
        for path in ("/v1/chat/completions", "/v1/responses"):
            _body, error = apply_request_policy(path, {"model": "gemini-omni-flash-preview"})
            self.assertIsNotNone(error)
            self.assertEqual(error.code, "OMNI_REQUIRES_DURABLE_JOB")


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

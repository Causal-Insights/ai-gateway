"""ModelArk stream completion, ordering, usage and partial-result regressions."""
import json
import unittest

from custom_handler_seedream import SeedreamLLM, parse_image_events


def stream(*events):
    return "\r\n\r\n".join("data: " + json.dumps(event) for event in events)


class SeedreamStreamingTests(unittest.TestCase):
    def test_ordered_results_and_terminal_usage(self):
        body = parse_image_events(stream(
            {"type": "image_generation.partial_succeeded", "image_index": 1, "url": "https://example.test/second", "size": "2048x2048"},
            {"type": "image_generation.partial_succeeded", "image_index": 0, "b64_json": "YWJj", "size": "1024x1024"},
            {"type": "image_generation.completed", "model": "seedream-5-0-lite-260128", "usage": {"output_tokens": 42}},
        ))
        result = SeedreamLLM._image_response_from_body(body)
        self.assertEqual(result.data[0].b64_json, "YWJj")
        self.assertEqual(result.data[1].url, "https://example.test/second")
        self.assertEqual(result._hidden_params["gateway_usage"]["output_tokens"], 42)
        self.assertEqual(result._hidden_params["gateway_usage"]["output_images"], 2)
        self.assertTrue(result.stream_completed)

    def test_partial_failure_retains_success(self):
        body = parse_image_events(stream(
            {"type": "image_generation.partial_succeeded", "image_index": 0, "url": "https://example.test/one"},
            {"type": "image_generation.partial_failed", "image_index": 1, "error": {"message": "filtered"}},
            {"type": "image_generation.completed", "usage": {"generated_images": 1}},
        ))
        result = SeedreamLLM._image_response_from_body(body)
        self.assertEqual(len(result.data), 1)
        self.assertEqual(result.errors[0]["image_index"], 1)

    def test_incomplete_stream_preserves_result_without_inventing_usage(self):
        body = parse_image_events(stream({"type": "image_generation.partial_succeeded", "image_index": 0, "url": "https://example.test/one"}))
        result = SeedreamLLM._image_response_from_body(body)
        self.assertFalse(result.stream_completed)
        self.assertNotIn("output_tokens", result._hidden_params["gateway_usage"])

    def test_error_only_keeps_provider_error(self):
        body = parse_image_events(stream({"type": "error", "error": {"message": "invalid size"}}))
        with self.assertRaisesRegex(Exception, "invalid size"):
            SeedreamLLM._image_response_from_body(body)

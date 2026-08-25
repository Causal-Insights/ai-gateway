import importlib.metadata
import importlib.util
import os
import unittest
from unittest.mock import patch


def _litellm_is_installed() -> bool:
    # Other isolated unit tests install a lightweight ``litellm`` stub.  Such a
    # module has no import spec, and find_spec() raises instead of returning
    # None when unittest discovery happens to import the stub first.
    try:
        return importlib.util.find_spec("litellm") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


@unittest.skipUnless(_litellm_is_installed(), "runs inside the LiteLLM application image")
class LiteLLMCompatibilityTests(unittest.TestCase):
    def test_exact_litellm_version_is_installed(self):
        self.assertEqual(importlib.metadata.version("litellm"), "1.95.0")

    def test_gateway_private_import_contract(self):
        from litellm.litellm_core_utils.litellm_logging import Logging
        from litellm.proxy._types import LiteLLMRoutes
        from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
        from litellm.proxy.proxy_server import app, llm_router
        from litellm.types.utils import ImageObject, ImageResponse

        self.assertTrue(callable(Logging))
        self.assertTrue(hasattr(LiteLLMRoutes.openai_routes, "value"))
        self.assertTrue(hasattr(LiteLLMRoutes.llm_api_routes, "value"))
        self.assertTrue(callable(user_api_key_auth))
        self.assertIsNotNone(UserAPIKeyAuth)
        self.assertIsNotNone(app)
        self.assertTrue(llm_router is None or hasattr(llm_router, "avideo_generation"))
        self.assertTrue(callable(ImageObject))
        self.assertTrue(callable(ImageResponse))

    def test_gateway_modules_import_against_target_image(self):
        import custom_handler  # noqa: F401
        import gateway_server  # noqa: F401
        import generation_job_adapters  # noqa: F401
        import generation_job_routes  # noqa: F401

    def test_seedream_private_fields_survive_litellm_image_dispatch(self):
        from custom_handler_seedream import SeedreamLLM
        from gateway_request_policy import apply_request_policy
        from litellm.utils import get_optional_params_image_gen

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
        optional = get_optional_params_image_gen(
            model="dola-seedream-5-0-pro-260628",
            n=body["n"],
            response_format=body["response_format"],
            size=body["size"],
            custom_llm_provider="seedream",
            output_format=body["output_format"],
            seedream_size=body["seedream_size"],
            seedream_output_count=body["seedream_output_count"],
            seedream_response_format=body["seedream_response_format"],
            seedream_output_format=body["seedream_output_format"],
        )
        self.assertEqual(optional["seedream_size"], "2816x1584")
        self.assertEqual(optional["seedream_output_count"], 1)

        with patch.dict(os.environ, {"BYTEDANCE_API_KEY": "test-key"}):
            _url, payload, _headers = SeedreamLLM()._prepare_request(
                "A wide editorial illustration",
                "dola-seedream-5-0-pro-260628",
                optional,
            )
        self.assertEqual(payload["size"], "2816x1584")
        self.assertEqual(payload["n"], 1)
        self.assertEqual(payload["output_format"], "png")


if __name__ == "__main__":
    unittest.main()

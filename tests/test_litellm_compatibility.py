import importlib.metadata
import importlib.util
import unittest


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


if __name__ == "__main__":
    unittest.main()

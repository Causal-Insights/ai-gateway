import io
import logging
import os
import unittest
from unittest.mock import patch

import gateway_healthcheck
from gateway_logging import _RegisterModelCacheWarningFilter, configure_gateway_logging


class _Response:
    status = 200

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self._body


class GatewayRuntimeTests(unittest.TestCase):
    def test_healthcheck_accepts_litellm_liveliness_response(self):
        with patch.dict(os.environ, {"LITELLM_MASTER_KEY": "sk-test", "PORT": "4000"}), patch(
            "gateway_healthcheck.urlopen", return_value=_Response(b'"I\'m alive!"')
        ) as request:
            self.assertEqual(gateway_healthcheck.main(), 0)
        self.assertEqual(request.call_args.args[0].get_header("Authorization"), "Bearer sk-test")

    def test_healthcheck_fails_without_master_key(self):
        with patch.dict(os.environ, {}, clear=True), patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(gateway_healthcheck.main(), 1)

    def test_logging_filter_is_narrowly_scoped(self):
        filter_ = _RegisterModelCacheWarningFilter()
        noisy = logging.LogRecord(
            "LiteLLM",
            logging.WARNING,
            __file__,
            1,
            "register_model: model=custom not in built-in cost map and no prefix/region variant "
            "matched; cache cost fields will default to 0",
            (),
            None,
        )
        other = logging.LogRecord(
            "LiteLLM", logging.WARNING, __file__, 1, "provider authentication failed", (), None
        )
        self.assertFalse(filter_.filter(noisy))
        self.assertTrue(filter_.filter(other))

    def test_logging_configuration_is_idempotent(self):
        logger = logging.getLogger("LiteLLM")
        original = list(logger.filters)
        try:
            with patch.dict(
                os.environ,
                {"SUPPRESS_LITELLM_REGISTER_MODEL_CACHE_WARNINGS": "true"},
            ):
                configure_gateway_logging()
                configure_gateway_logging()
            matching = [item for item in logger.filters if isinstance(item, _RegisterModelCacheWarningFilter)]
            self.assertEqual(len(matching), 1)
        finally:
            logger.filters[:] = original


if __name__ == "__main__":
    unittest.main()

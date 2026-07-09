import base64
import os
import sys
import types
import unittest
from unittest.mock import patch

import httpx


def _ensure_litellm_stubs():
    if "litellm" in sys.modules:
        return

    class CustomLLM:
        pass

    class ImageObject:
        def __init__(self, url=None, b64_json=None, **kwargs):
            self.url = url
            self.b64_json = b64_json
            for key, value in kwargs.items():
                setattr(self, key, value)

    class ImageResponse:
        def __init__(self, created=0, data=None, **kwargs):
            self.created = created
            self.data = data if data is not None else []
            self._hidden_params = {}

    litellm = types.ModuleType("litellm")
    litellm.CustomLLM = CustomLLM
    sys.modules["litellm"] = litellm
    http_handler = types.ModuleType("litellm.llms.custom_httpx.http_handler")
    http_handler.AsyncHTTPHandler = type("AsyncHTTPHandler", (), {})
    http_handler.HTTPHandler = type("HTTPHandler", (), {})
    sys.modules["litellm.llms.custom_httpx.http_handler"] = http_handler
    sys.modules.setdefault("litellm.llms", types.ModuleType("litellm.llms"))
    sys.modules.setdefault("litellm.llms.custom_httpx", types.ModuleType("litellm.llms.custom_httpx"))
    types_mod = types.ModuleType("litellm.types.utils")
    types_mod.ImageObject = ImageObject
    types_mod.ImageResponse = ImageResponse
    sys.modules["litellm.types.utils"] = types_mod
    sys.modules.setdefault("litellm.types", types.ModuleType("litellm.types"))


_ensure_litellm_stubs()

from custom_handler_audio import AudioStudioLLM


class _AsyncClient:
    def __init__(self, response):
        self.response = response
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        self.request = {"url": url, "headers": headers, "json": json}
        return self.response


class AudioStudioHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_generates_sound_effect_audio(self):
        request = httpx.Request("POST", "https://api.elevenlabs.io/v1/sound-generation")
        response = httpx.Response(200, content=b"sfx-bytes", request=request, headers={"content-type": "audio/mpeg"})
        client = _AsyncClient(response)

        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}), patch(
            "custom_handler_audio.httpx.AsyncClient", return_value=client
        ):
            result = await AudioStudioLLM().aimage_generation(
                model="elevenlabs-sfx",
                prompt="door slam",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"duration_seconds": 2},
                logging_obj=None,
            )

        self.assertEqual(client.request["json"]["model_id"], "eleven_text_to_sound_v2")
        self.assertEqual(client.request["json"]["duration_seconds"], 2)
        self.assertEqual(base64.b64decode(result.data[0].b64_json), b"sfx-bytes")

    async def test_generates_music_audio(self):
        request = httpx.Request("POST", "https://api.elevenlabs.io/v1/music")
        response = httpx.Response(200, content=b"music-bytes", request=request, headers={"content-type": "audio/mpeg"})
        client = _AsyncClient(response)

        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-key"}), patch(
            "custom_handler_audio.httpx.AsyncClient", return_value=client
        ):
            result = await AudioStudioLLM().aimage_generation(
                model="elevenlabs-music",
                prompt="cinematic score",
                model_response=None,
                api_key=None,
                api_base=None,
                optional_params={"music_length_ms": 5000},
                logging_obj=None,
            )

        self.assertEqual(client.request["json"]["model_id"], "music_v1")
        self.assertEqual(client.request["json"]["music_length_ms"], 5000)
        self.assertEqual(base64.b64decode(result.data[0].b64_json), b"music-bytes")


if __name__ == "__main__":
    unittest.main()

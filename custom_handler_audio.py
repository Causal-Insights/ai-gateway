"""ElevenLabs SFX and Music custom LiteLLM image-generation handlers.

LiteLLM's standard speech endpoint does not cover these provider-native APIs.
The handlers return base64 audio in an ImageResponse so callers can use the
proxy's authenticated /images/generations route without exposing provider keys.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any, Optional, Union

import httpx
from litellm import CustomLLM
from litellm.types.utils import ImageObject, ImageResponse

from custom_handler_common import normalize_error


class AudioStudioException(Exception):
    """Raised when an Audio Studio provider request fails."""


class AudioStudioLLM(CustomLLM):
    ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"
    MAX_AUDIO_BYTES = 100 * 1024 * 1024

    @staticmethod
    def _mode(model: str) -> str:
        normalized = (model or "").strip().lower()
        if normalized.endswith("elevenlabs-sfx"):
            return "sfx"
        if normalized.endswith("elevenlabs-music"):
            return "music"
        raise ValueError("Audio Studio model must be elevenlabs-sfx or elevenlabs-music")

    @staticmethod
    def _optional_value(optional_params: dict, key: str) -> Any:
        value = optional_params.get(key)
        return value if value is not None else None

    def _prepare_request(self, prompt: str, model: str, optional_params: dict) -> tuple[str, dict, dict]:
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            raise ValueError("ELEVENLABS_API_KEY is required for Audio Studio generation")
        prompt_text = (prompt or "").strip()
        if not prompt_text:
            raise ValueError("prompt is required for Audio Studio generation")

        mode = self._mode(model)
        params = dict(optional_params or {})
        if mode == "sfx":
            payload = {
                "text": prompt_text,
                "model_id": "eleven_text_to_sound_v2",
            }
            for key in ("duration_seconds", "prompt_influence", "loop"):
                value = self._optional_value(params, key)
                if value is not None:
                    payload[key] = value
            url = f"{self.ELEVENLABS_BASE}/sound-generation"
        else:
            payload = {
                "prompt": prompt_text,
                "model_id": "music_v1",
            }
            for key in ("music_length_ms", "force_instrumental"):
                value = self._optional_value(params, key)
                if value is not None:
                    payload[key] = value
            url = f"{self.ELEVENLABS_BASE}/music"

        return url, payload, {
            "xi-api-key": api_key,
            "content-type": "application/json",
            "accept": "audio/mpeg",
        }

    async def aimage_generation(
        self,
        model: str,
        prompt: str,
        model_response: ImageResponse,
        api_key: Optional[str],
        api_base: Optional[str],
        optional_params: dict,
        logging_obj: Any,
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        client: Any = None,
        **kwargs: Any,
    ) -> ImageResponse:
        url, payload, headers = self._prepare_request(prompt, model, optional_params)
        async with httpx.AsyncClient(timeout=timeout or 300) as http:
            response = await http.post(url, headers=headers, json=payload)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                try:
                    detail = error.response.json()
                except Exception:
                    detail = error.response.text
                raise AudioStudioException(normalize_error(detail)) from error
            if not response.content:
                raise AudioStudioException("ElevenLabs returned an empty audio file")
            if len(response.content) > self.MAX_AUDIO_BYTES:
                raise AudioStudioException("ElevenLabs audio output exceeds the 100 MB limit")
            return ImageResponse(
                created=int(time.time()),
                data=[ImageObject(b64_json=base64.b64encode(response.content).decode("ascii"))],
            )


audio_studio = AudioStudioLLM()

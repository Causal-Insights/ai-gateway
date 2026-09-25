"""Request-correction regressions against the pinned SDK; no provider or DB calls."""
import importlib.metadata
import os
import unittest
from email.parser import BytesParser
from email.policy import default
from unittest.mock import AsyncMock, MagicMock, patch

from generation_job_adapters import BytePlusAdapter, ProviderAdapterError, XAIAdapter
from generation_job_models import GenerationJobCreate
from gateway_request_policy import apply_request_policy


class SeedanceCorrections(unittest.IsolatedAsyncioTestCase):
    async def submit(self, **values):
        request = GenerationJobCreate(model="seedance-2.5", prompt="Animate the scene", **values)
        with patch.dict(os.environ, {"BYTEDANCE_API_KEY": "offline-test"}), patch(
            "generation_job_adapters._json_request", AsyncMock(return_value={"id": "task-fixture"})
        ) as post:
            result = await BytePlusAdapter().submit(request, job_id="gen_fixture", callback_url=None)
        post.assert_awaited_once()
        return post.await_args.kwargs["body"], result

    async def test_automatic_duration_is_preserved_on_wire_and_submission_metadata(self):
        body, result = await self.submit(duration_seconds=-1)
        self.assertEqual(body["duration"], -1)
        self.assertEqual(result.request_metadata["duration_seconds"], -1)
        self.assertEqual(body["model"], "dreamina-seedance-2-5-260628")

    async def test_edit_and_auto_source_use_exact_provider_operation_and_inherited_defaults(self):
        source = [{"type": "video", "role": "source", "url": "https://assets.example/source.mp4"}]
        for operation in ("edit", "auto"):
            with self.subTest(operation=operation):
                body, result = await self.submit(operation=operation, media_inputs=source)
                self.assertEqual(body["omni_reference_task_type"], "edit")
                self.assertNotIn("task_type", body)
                self.assertEqual(body["duration"], -1)
                self.assertEqual(body["ratio"], "adaptive")
                self.assertEqual(body["content"][1]["video_url"]["url"], source[0]["url"])
                self.assertEqual(result.request_metadata["duration_seconds"], -1)
                self.assertTrue(result.request_metadata["has_input_video"])

    async def test_edit_preserves_explicit_supported_ratio(self):
        body, _ = await self.submit(operation="edit", duration_seconds=-1, aspect_ratio="16:9",
            media_inputs=[{"type": "video", "role": "source", "url": "https://assets.example/source.mp4"}])
        self.assertEqual(body["ratio"], "16:9")
        self.assertEqual(body["duration"], -1)

    async def test_edit_rejects_fixed_duration_before_submission(self):
        request = GenerationJobCreate(model="seedance-2.5", prompt="Edit this scene", operation="edit",
            duration_seconds=4, media_inputs=[{"type": "video", "role": "source", "url": "https://assets.example/source.mp4"}])
        with patch.dict(os.environ, {"BYTEDANCE_API_KEY": "offline-test"}), patch(
            "generation_job_adapters._json_request", AsyncMock()
        ) as post:
            with self.assertRaisesRegex(ProviderAdapterError, "duration"):
                await BytePlusAdapter().submit(request, job_id="gen_fixture", callback_url=None)
        post.assert_not_awaited()

    async def test_first_frame_and_extension_inherit_aspect_ratio(self):
        cases = [
            {"media_inputs": [{"type": "image", "role": "first_frame", "url": "https://assets.example/frame.png"}]},
            {"operation": "extend", "media_inputs": [{"type": "video", "role": "source", "url": "https://assets.example/source.mp4"}]},
        ]
        for values in cases:
            with self.subTest(values=values):
                body, _ = await self.submit(**values)
                self.assertEqual(body["ratio"], "adaptive")

    async def test_existing_text_defaults_are_preserved(self):
        body, result = await self.submit()
        self.assertEqual(body["duration"], 4)
        self.assertEqual(body["ratio"], "1:1")
        self.assertEqual(result.request_metadata["duration_seconds"], 4)

    def test_automatic_duration_does_not_expand_other_models_or_accept_zero(self):
        for model, duration in (("seedance-2.0", -1), ("grok-video-1.5", -1), ("seedance-2.5", 0)):
            with self.subTest(model=model, duration=duration), self.assertRaises(ValueError):
                GenerationJobCreate(model=model, duration_seconds=duration)


class GrokCorrections(unittest.IsolatedAsyncioTestCase):
    async def test_v1_audio_preserves_explicit_false_true_and_omission(self):
        for settings in ({}, {"generate_audio": False}, {"generate_audio": True}):
            with self.subTest(settings=settings):
                request = GenerationJobCreate(model="grok-video-1.5", prompt="A moving light", duration_seconds=1, **settings)
                with patch.dict(os.environ, {"GROK_API_KEY": "offline-test"}), patch(
                    "generation_job_adapters._json_request", AsyncMock(return_value={"request_id": "req-fixture"})
                ) as post:
                    await XAIAdapter().submit(request, job_id="gen_fixture", callback_url=None)
                body = post.await_args.kwargs["body"]
                if "generate_audio" in settings:
                    self.assertIs(body["generate_audio"], settings["generate_audio"])
                else:
                    self.assertNotIn("generate_audio", body)
                post.assert_awaited_once()

    async def test_auto_quality_and_equivalent_wide_sizes_survive_policy_and_handler(self):
        from custom_handler_xai import GrokImageLLM
        sizes = {"21:9": "21:9", "2100x900": "21:9", "19.5:9": "19.5:9", "1950x900": "19.5:9"}
        for size, ratio in sizes.items():
            with self.subTest(size=size):
                body, error = apply_request_policy("/v1/images/generations", {
                    "model": "grok-imagine-image-2.0", "prompt": "A wide scene", "size": size,
                    "quality": "auto", "response_format": "b64_json"})
                self.assertIsNone(error)
                self.assertEqual(body["aspect_ratio"], ratio)
                self.assertNotIn("size", body)
                response = MagicMock()
                response.raise_for_status = MagicMock()
                response.json.return_value = {"data": [{"b64_json": "aW1hZ2U="}]}
                client = AsyncMock()
                client.post.return_value = response
                client.__aenter__.return_value = client
                options = {key: value for key, value in body.items() if key not in {"model", "prompt"}}
                with patch.dict(os.environ, {"GROK_API_KEY": "offline-test"}), patch(
                    "custom_handler_xai.httpx.AsyncClient", return_value=client):
                    output = await GrokImageLLM().aimage_generation(model="grok-imagine-image-2.0",
                        prompt=body["prompt"], model_response=None, api_key=None, api_base=None,
                        optional_params=options, logging_obj=None)
                submitted = client.post.await_args.kwargs["json"]
                self.assertEqual(submitted["quality"], "auto")
                self.assertEqual(submitted["aspect_ratio"], ratio)
                self.assertEqual(output.data[0].b64_json, "aW1hZ2U=")
                client.post.assert_awaited_once()


class OlderGPTImageEdits(unittest.IsolatedAsyncioTestCase):
    async def test_installed_sdk_transmits_old_edit_controls_mask_and_ordered_references(self):
        import httpx
        import litellm
        from openai_model_contracts import install_image_adapters
        self.assertEqual(importlib.metadata.version("litellm"), "1.102.1")
        install_image_adapters()
        for model in ("gpt-image-1.5", "gpt-image-2", "gpt-image-2-2026-04-21"):
            with self.subTest(model=model):
                captured = []

                async def send(client, request, **kwargs):
                    raw = await request.aread()
                    message = BytesParser(policy=default).parsebytes(
                        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + raw)
                    fields, images, masks = {}, [], []
                    for part in message.iter_parts():
                        name = part.get_param("name", header="content-disposition")
                        value = part.get_payload(decode=True)
                        if name in ("image", "image[]"):
                            images.append(value)
                        elif name == "mask":
                            masks.append(value)
                        else:
                            fields[name] = value.decode()
                    captured.append((str(request.url), fields, images, masks))
                    return httpx.Response(200, request=request, json={"created": 1,
                        "data": [{"b64_json": "aW1hZ2U="}], "output_format": "webp"})

                expected = {"quality": "low", "size": "1024x1024", "output_format": "webp",
                            "output_compression": 80, "background": "transparent", "moderation": "low", "n": 1}
                with patch.object(litellm, "callbacks", []), patch("httpx.AsyncClient.send", new=send):
                    result = await litellm.aimage_edit(model="openai/" + model,
                        prompt="Keep the subject and edit the mask", image=[b"first-reference", b"second-reference"],
                        mask=b"edit-mask", api_key="offline-test", num_retries=0, **expected)
                self.assertEqual(len(captured), 1)
                url, fields, images, masks = captured[0]
                self.assertTrue(url.endswith("/images/edits"))
                self.assertEqual(fields["model"], model)
                for name, value in expected.items():
                    self.assertEqual(fields[name], str(value), name)
                self.assertEqual(images, [b"first-reference", b"second-reference"])
                self.assertEqual(masks, [b"edit-mask"])
                self.assertEqual(result.data[0].b64_json, "aW1hZ2U=")


if __name__ == "__main__":
    unittest.main()

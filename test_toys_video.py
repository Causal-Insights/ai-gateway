#!/usr/bin/env python3
"""
Animate test_image_toys.jpg into a portrait (3:4) video via the LiteLLM proxy.

Flow:
    1. Upload test_image_toys.jpg to xAI Files API to obtain a file_id.
    2. POST {LITELLM_API_BASE}/v1/images/generations with the file_id and
       aspect_ratio="3:4" so the output is portrait-mode.
    3. The custom grok-video handler polls xAI until generation is complete
       and returns a video URL inside the LiteLLM response.
    4. Download the MP4 and save it locally.

Requires:  LITELLM_MASTER_KEY, GROK_API_KEY
Optional:  LITELLM_API_BASE  (default: http://localhost:4000)
           GROK_VIDEO_MODEL  (default: grok-imagine-video)
           GROK_VIDEO_DURATION (seconds, default: 5)
Usage:
    python test_toys_video.py
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

LITELLM_API_BASE = os.environ.get("LITELLM_API_BASE", "http://localhost:4000").rstrip("/")
MODEL = "grok-video"
UPSTREAM_MODEL = os.environ.get("GROK_VIDEO_MODEL", "grok-imagine-video")
DURATION = int(os.environ.get("GROK_VIDEO_DURATION", "5"))
IMAGE_PATH = os.path.join(os.path.dirname(__file__), "test_image_toys.jpg")
OUTPUT_FILE = "test_toys_video.mp4"
ASPECT_RATIO = "3:4"

PROMPT = (
    "Bring the toys in <image_1> to life. "
    "Animate them with playful, gentle motion — make them bounce, spin, or interact "
    "with each other in a cheerful, whimsical way. "
    "Keep the scene bright and colourful."
)


def _upload_image(image_path: str, api_key: str) -> str:
    """Upload a local image to the xAI Files API and return the file_id."""
    filename = os.path.basename(image_path)
    ext = os.path.splitext(filename)[1].lstrip(".").lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext, f"image/{ext}")

    with open(image_path, "rb") as f:
        raw = f.read()

    with httpx.Client(timeout=60) as client:
        resp = client.post(
            "https://api.x.ai/v1/files",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (filename, raw, mime)},
        )

    if resp.status_code != 200:
        sys.exit(f"xAI file upload failed {resp.status_code}: {resp.text}")

    file_id = resp.json().get("id")
    if not file_id:
        sys.exit(f"xAI file upload returned no id: {resp.text}")
    return file_id


def main() -> None:
    master_key = os.environ.get("LITELLM_MASTER_KEY")
    if not master_key:
        sys.exit("ERROR: set LITELLM_MASTER_KEY")

    grok_api_key = os.environ.get("GROK_API_KEY")
    if not grok_api_key:
        sys.exit("ERROR: set GROK_API_KEY (needed to upload the image to xAI Files API)")

    if not os.path.exists(IMAGE_PATH):
        sys.exit(f"ERROR: image not found at {IMAGE_PATH!r}")

    # ── 1. Upload source image ─────────────────────────────────────────────
    print(f"[upload] {IMAGE_PATH!r} → xAI Files API")
    file_id = _upload_image(IMAGE_PATH, grok_api_key)
    print(f"[upload] file_id={file_id!r}")

    # ── 2. Request video generation ────────────────────────────────────────
    payload = {
        "model": MODEL,
        "prompt": PROMPT,
        "duration": DURATION,
        "aspect_ratio": ASPECT_RATIO,
        "xai_model": UPSTREAM_MODEL,
        "image_file_id": file_id,
    }

    print(f"[submit] proxy={LITELLM_API_BASE!r}")
    print(f"[submit] model={MODEL!r}  upstream={UPSTREAM_MODEL!r}  "
          f"duration={DURATION}s  aspect_ratio={ASPECT_RATIO}")
    print(f"         prompt={PROMPT!r}")

    with httpx.Client(timeout=660) as client:
        resp = client.post(
            f"{LITELLM_API_BASE}/v1/images/generations",
            headers={
                "Authorization": f"Bearer {master_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if resp.status_code != 200:
        sys.exit(f"LiteLLM request failed {resp.status_code}: {resp.text}")

    data = resp.json()
    outputs = data.get("data") or []
    if not outputs:
        sys.exit(f"No data array in response: {data}")

    video_url = outputs[0].get("url")
    if not video_url:
        sys.exit(f"No url in first data item: {data}")

    print("[done]   received video URL from LiteLLM")
    print(f"[done]   url={video_url}")

    # ── 3. Download and save ───────────────────────────────────────────────
    with httpx.Client(timeout=120) as client:
        dl = client.get(video_url)
        dl.raise_for_status()

    with open(OUTPUT_FILE, "wb") as f:
        f.write(dl.content)

    size_kb = len(dl.content) / 1024
    print(f"[save]   {OUTPUT_FILE}  ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()

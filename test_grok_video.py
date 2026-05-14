#!/usr/bin/env python3
"""
Test Grok video generation through the LiteLLM proxy.

Flow:
    1. POST {LITELLM_API_BASE}/v1/images/generations
    2. Read data[0].url from LiteLLM response (custom handler already polled upstream)
    3. Download the MP4 from that URL

Requires: LITELLM_MASTER_KEY env var.
Optional: LITELLM_API_BASE, GROK_VIDEO_MODEL, GROK_VIDEO_DURATION, GROK_VIDEO_PROMPT
Usage:    python test_grok_video.py
"""

import os
import sys

import httpx

from dotenv import load_dotenv

load_dotenv()
LITELLM_API_BASE = os.environ.get("LITELLM_API_BASE", "http://localhost:4000").rstrip("/")
MODEL = "grok-video"
PROMPT = os.environ.get(
    "GROK_VIDEO_PROMPT",
    "A red ball bouncing once on a white surface, minimal scene",
)
DURATION = int(os.environ.get("GROK_VIDEO_DURATION", "1"))
UPSTREAM_MODEL = os.environ.get("GROK_VIDEO_MODEL", "grok-imagine-video")
OUTPUT_FILE = "test_grok_video.mp4"


def main() -> None:
    master_key = os.environ.get("LITELLM_MASTER_KEY")
    if not master_key:
        sys.exit("ERROR: set LITELLM_MASTER_KEY")

    payload = {
        "model": MODEL,
        "prompt": PROMPT,
        "duration": DURATION,
        # The proxy alias routes to the custom grok-video provider; this keeps
        # the upstream xAI model configurable per test run.
        "xai_model": UPSTREAM_MODEL,
    }
    print(f"[submit] proxy={LITELLM_API_BASE!r}")
    print(f"[submit] model={MODEL!r} xai_model={UPSTREAM_MODEL!r} duration={DURATION}s")
    print(f"         prompt={PROMPT!r}")

    with httpx.Client(timeout=300) as client:
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

    # ── 2. Download and save ───────────────────────────────────────────────
    with httpx.Client(timeout=120) as client:
        dl = client.get(video_url)
        dl.raise_for_status()
    with open(OUTPUT_FILE, "wb") as f:
        f.write(dl.content)
    size_kb = len(dl.content) / 1024
    print(f"[save]   {OUTPUT_FILE}  ({size_kb:.1f} KB)")


def _upload_image_to_xai(image_path: str, api_key: str) -> str:
    """Upload a local image to xAI Files API and return the file_id."""
    XAI_FILES_URL = "https://api.x.ai/v1/files"
    with open(image_path, "rb") as f:
        raw = f.read()
    filename = os.path.basename(image_path)
    ext = os.path.splitext(filename)[1].lstrip(".").lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(ext, f"image/{ext}")
    with httpx.Client(timeout=60) as client:
        resp = client.post(
            XAI_FILES_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (filename, raw, mime)},
        )
    if resp.status_code != 200:
        sys.exit(f"xAI file upload failed {resp.status_code}: {resp.text}")
    file_id = resp.json().get("id")
    if not file_id:
        sys.exit(f"xAI file upload returned no id: {resp.text}")
    return file_id


def storyboard_to_video() -> None:
    """Upload jason_cartoon.png to xAI, then animate it as a 6-second video."""
    master_key = os.environ.get("LITELLM_MASTER_KEY")
    if not master_key:
        sys.exit("ERROR: set LITELLM_MASTER_KEY")
    grok_api_key = os.environ.get("GROK_API_KEY")
    if not grok_api_key:
        sys.exit("ERROR: set GROK_API_KEY (needed to upload image to xAI Files API)")

    image_path = os.path.join(os.path.dirname(__file__), "jason_cartoon.png")
    if not os.path.exists(image_path):
        sys.exit(f"ERROR: image not found at {image_path}")

    print(f"[upload] uploading {image_path!r} to xAI Files API...")
    file_id = _upload_image_to_xai(image_path, grok_api_key)
    print(f"[upload] file_id={file_id!r}")

    upstream_model = os.environ.get("GROK_VIDEO_MODEL", "grok-imagine-video")
    output_file = "test_grok_storyboard_video.mp4"
    prompt = (
        "Turn the attached storyboard in <image_1> into an animated video. "
        "Scan through each beat in the storyboard. "
        "Zoom in and out as you move through to each beat."
    )

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "duration": 6,
        "xai_model": upstream_model,
        "image_file_id": file_id,
    }

    print(f"[submit] proxy={LITELLM_API_BASE!r}")
    print(f"[submit] model={MODEL!r} xai_model={upstream_model!r} duration=6s")
    print(f"         image={image_path!r}")
    print(f"         prompt={prompt!r}")

    with httpx.Client(timeout=300) as client:
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

    with httpx.Client(timeout=120) as client:
        dl = client.get(video_url)
        dl.raise_for_status()
    with open(output_file, "wb") as f:
        f.write(dl.content)
    size_kb = len(dl.content) / 1024
    print(f"[save]   {output_file}  ({size_kb:.1f} KB)")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test Grok video via LiteLLM proxy")
    parser.add_argument(
        "test",
        nargs="?",
        choices=["text", "storyboard"],
        default="text",
        help="Which test to run: 'text' (default) or 'storyboard' (image-to-video)",
    )
    args = parser.parse_args()

    if args.test == "storyboard":
        storyboard_to_video()
    else:
        main()

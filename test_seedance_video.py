#!/usr/bin/env python3
"""
Test Seedance 2.0 video generation through the LiteLLM proxy.

Flow:
    1. POST {LITELLM_API_BASE}/v1/images/generations
    2. Read data[0].url from LiteLLM response (custom handler already polled upstream)
    3. Download the MP4 from that URL

Requires: LITELLM_MASTER_KEY env var.
Optional: LITELLM_API_BASE, SEEDANCE_PROMPT, SEEDANCE_DURATION,
                    SEEDANCE_RESOLUTION, SEEDANCE_RATIO, SEEDANCE_GENERATE_AUDIO,
                    SEEDANCE_WATERMARK
Usage:    python test_seedance_video.py
"""

import os
import sys

from dotenv import load_dotenv

import httpx

load_dotenv()

LITELLM_API_BASE = os.environ.get("LITELLM_API_BASE", "http://localhost:4000").rstrip("/")
MODEL = "seedance-2.0"
PROMPT = (
        os.environ.get(
                "SEEDANCE_PROMPT",
                "A person walking through a forest at night. cinematic lighting. Intense",
        )
)
DURATION = int(os.environ.get("SEEDANCE_DURATION", "4"))
RESOLUTION = os.environ.get("SEEDANCE_RESOLUTION", "480p")
RATIO = os.environ.get("SEEDANCE_RATIO", "1:1")
GENERATE_AUDIO = os.environ.get("SEEDANCE_GENERATE_AUDIO", "false").lower() == "true"
WATERMARK = os.environ.get("SEEDANCE_WATERMARK", "false").lower() == "true"
OUTPUT_FILE = "test_seedance_vampire_2.mp4"


def main() -> None:
    master_key = os.environ.get("LITELLM_MASTER_KEY")
    if not master_key:
        sys.exit("ERROR: set LITELLM_MASTER_KEY")

    payload = {
        "model": MODEL,
        "prompt": PROMPT,
        "resolution": RESOLUTION,
        "ratio": RATIO,
        "duration": DURATION,
        "generate_audio": GENERATE_AUDIO,
        "watermark": WATERMARK,
    }

    print(f"[submit] proxy={LITELLM_API_BASE!r}")
    print(f"[submit] model={MODEL!r}")
    print(f"         {RESOLUTION} {RATIO} {DURATION}s  audio={GENERATE_AUDIO}")
    print(f"         watermark={WATERMARK}")
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


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Seedance 2.0 reference-image test through the LiteLLM proxy.

Reads ``jason_cartoon.png`` from this directory, sends it as a ``data:image/png;base64,...``
URL as a reference image.

Flow:
  1. POST {LITELLM_API_BASE}/v1/images/generations
  2. Read data[0].url from LiteLLM response (custom handler already polled upstream)
  3. Download the MP4 from that URL

Requires: LITELLM_MASTER_KEY

Usage:
    python test_seedance_cartoon_reference.py

Optional env:
    LITELLM_API_BASE      default: http://localhost:4000
    SEEDANCE_REFERENCE_IMAGE  absolute or relative path override (default: ./jason_cartoon.png)
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

import httpx

load_dotenv()

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_IMAGE = SCRIPT_DIR / "jason_cartoon.png"

LITELLM_API_BASE = os.environ.get("LITELLM_API_BASE", "http://localhost:4000").rstrip("/")
MODEL = "seedance-2.0-fast"

PROMPT = (
    "Recreate this cartoon scene faithfully—same characters, composition, and style—with "
    "gentle subtle motion and soft cinematic lighting. Keep it playful and readable."
)

DURATION = 4
RESOLUTION = "480p"
RATIO = "1:1"
OUTPUT_FILE = "test_seedance_cartoon_reference.mp4"


def _png_to_data_uri(path: Path) -> str:
    raw = path.read_bytes()
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{b64}"


def main() -> None:
    master_key = os.environ.get("LITELLM_MASTER_KEY")
    if not master_key:
        sys.exit("ERROR: set LITELLM_MASTER_KEY")

    img_path = Path(os.environ.get("SEEDANCE_REFERENCE_IMAGE", DEFAULT_IMAGE)).expanduser()
    if not img_path.is_file():
        sys.exit(
            f"ERROR: reference image not found: {img_path}\n"
            f"Place jason_cartoon.png next to this script or set SEEDANCE_REFERENCE_IMAGE."
        )

    data_uri = _png_to_data_uri(img_path)
    print(f"[image]  {img_path}  ({len(data_uri) // 1024} KB base64 payload)")

    payload = {
        "model": MODEL,
        "prompt": PROMPT,
        "reference_image_urls": [data_uri],
        "resolution": RESOLUTION,
        "ratio": RATIO,
        "duration": DURATION,
        "generate_audio": False,
        "watermark": False,
    }

    print(f"[submit] proxy={LITELLM_API_BASE!r}")
    print(f"[submit] model={MODEL!r}")
    print(f"         {RESOLUTION} {RATIO} {DURATION}s  reference_image  audio=off")
    print(f"         prompt={PROMPT!r}")

    with httpx.Client(timeout=1200) as client:
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

    out_path = SCRIPT_DIR / OUTPUT_FILE

    with httpx.Client(timeout=120) as client:
        dl = client.get(video_url)
        dl.raise_for_status()
    out_path.write_bytes(dl.content)
    size_kb = len(dl.content) / 1024
    print(f"[save]   {out_path}  ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()

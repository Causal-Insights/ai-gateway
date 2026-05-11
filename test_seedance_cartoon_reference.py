#!/usr/bin/env python3
"""
Seedance 2.0 reference-image test: recreate a cartoon scene from a local PNG.

Reads ``jason_cartoon.png`` from this directory, sends it as a ``data:image/png;base64,...``
URL (ARK accepts data URIs), with ``role: reference_image``.

Requires: BYTEDANCE_API_KEY or ARK_API_KEY

Usage:
    python test_seedance_cartoon_reference.py

Optional env:
    SEEDANCE_REFERENCE_IMAGE  absolute or relative path override (default: ./jason_cartoon.png)
"""

from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

import httpx

load_dotenv()

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_IMAGE = SCRIPT_DIR / "jason_cartoon.png"

ARK_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"
MODEL = "dreamina-seedance-2-0-260128"

PROMPT = (
    "Recreate this cartoon scene faithfully—same characters, composition, and style—with "
    "gentle subtle motion and soft cinematic lighting. Keep it playful and readable."
)

DURATION = 4
RESOLUTION = "480p"
RATIO = "1:1"
POLL_INTERVAL = 10
POLL_TIMEOUT = 1200
OUTPUT_FILE = "test_seedance_cartoon_reference.mp4"


def _png_to_data_uri(path: Path) -> str:
    raw = path.read_bytes()
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{b64}"


def main() -> None:
    api_key = os.environ.get("BYTEDANCE_API_KEY") or os.environ.get("ARK_API_KEY")
    if not api_key:
        sys.exit("ERROR: set BYTEDANCE_API_KEY or ARK_API_KEY")

    img_path = Path(os.environ.get("SEEDANCE_REFERENCE_IMAGE", DEFAULT_IMAGE)).expanduser()
    if not img_path.is_file():
        sys.exit(
            f"ERROR: reference image not found: {img_path}\n"
            f"Place jason_cartoon.png next to this script or set SEEDANCE_REFERENCE_IMAGE."
        )

    data_uri = _png_to_data_uri(img_path)
    print(f"[image]  {img_path}  ({len(data_uri) // 1024} KB base64 payload)")

    auth_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": MODEL,
        "content": [
            {"type": "text", "text": PROMPT},
            {
                "type": "image_url",
                "image_url": {"url": data_uri},
                "role": "reference_image",
            },
        ],
        "resolution": RESOLUTION,
        "ratio": RATIO,
        "duration": DURATION,
        "generate_audio": False,
        "watermark": False,
    }

    print(f"[submit] model={MODEL!r}")
    print(f"         {RESOLUTION} {RATIO} {DURATION}s  reference_image  audio=off")
    print(f"         prompt={PROMPT!r}")

    with httpx.Client(timeout=120) as client:
        resp = client.post(
            f"{ARK_BASE}/contents/generations/tasks",
            headers=auth_headers,
            json=payload,
        )

    if resp.status_code != 200:
        sys.exit(f"Submit failed {resp.status_code}: {resp.text}")

    task_id = resp.json().get("id")
    if not task_id:
        sys.exit(f"No task id in response: {resp.json()}")
    print(f"[submit] task_id={task_id}")

    deadline = time.monotonic() + POLL_TIMEOUT
    out_path = SCRIPT_DIR / OUTPUT_FILE

    with httpx.Client(timeout=60) as client:
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            poll = client.get(
                f"{ARK_BASE}/contents/generations/tasks/{task_id}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if poll.status_code != 200:
                print(f"[poll]   HTTP {poll.status_code}: {poll.text}")
                continue

            data = poll.json()
            status = data.get("status", "unknown")
            print(f"[poll]   status={status}")

            if status == "failed":
                error = data.get("error", {})
                sys.exit(f"Generation failed: {error.get('message', data)}")

            if status == "expired":
                sys.exit("Task expired before completing.")

            if status == "succeeded":
                video_url = data["content"]["video_url"]
                actual_duration = data.get("duration", "?")
                actual_ratio = data.get("ratio", RATIO)

                print(f"\n[done]   duration={actual_duration}s  ratio={actual_ratio}")
                print(f"[done]   url={video_url}")

                dl = client.get(video_url)
                dl.raise_for_status()
                out_path.write_bytes(dl.content)
                size_kb = len(dl.content) / 1024
                print(f"[save]   {out_path}  ({size_kb:.1f} KB)")
                return

    sys.exit(f"Timed out after {POLL_TIMEOUT}s waiting for {task_id}")


if __name__ == "__main__":
    main()

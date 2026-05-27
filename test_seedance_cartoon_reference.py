#!/usr/bin/env python3
"""
Seedance 2.0 reference-image test through the LiteLLM proxy.

Reads ``jason_cartoon.png`` from this directory, sends it as a
``data:image/png;base64,...`` URL as a reference image. Uses the same
bounded-wait + polling pattern as ``test_seedance_video.py``.

Requires: LITELLM_MASTER_KEY

Usage:
    python test_seedance_cartoon_reference.py

Optional env:
    LITELLM_API_BASE          default: http://localhost:4000
    SEEDANCE_REFERENCE_IMAGE  absolute or relative path (default: ./jason_cartoon.png)
    SEEDANCE_MAX_POLL_S       max polling time once a task URL is returned (default 1200)
"""

from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

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
MAX_POLL_S = float(os.environ.get("SEEDANCE_MAX_POLL_S", "1200"))
POLL_INTERVAL_S = 10.0
OUTPUT_FILE = "test_seedance_cartoon_reference.mp4"

TASK_URL_PREFIX = "seedance-task://"


def _png_to_data_uri(path: Path) -> str:
    raw = path.read_bytes()
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _is_task_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith(TASK_URL_PREFIX)


def _task_id_from_url(url: str) -> str:
    return url[len(TASK_URL_PREFIX):]


def _post(master_key: str, payload: dict) -> dict:
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
    return resp.json()


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

    submit_payload = {
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

    data = _post(master_key, submit_payload)
    outputs = data.get("data") or []
    if not outputs:
        sys.exit(f"No data array in response: {data}")

    video_url = outputs[0].get("url")
    status = outputs[0].get("revised_prompt") or ""
    if not video_url:
        sys.exit(f"No url in first data item: {data}")

    if _is_task_url(video_url):
        task_id = _task_id_from_url(video_url)
        print(f"[task]   submitted as {task_id} (status={status or 'running'})")
        deadline = time.monotonic() + MAX_POLL_S
        while True:
            if time.monotonic() >= deadline:
                sys.exit(f"Task {task_id} did not complete within {MAX_POLL_S:.0f}s")
            time.sleep(POLL_INTERVAL_S)
            poll = _post(
                master_key,
                {
                    "model": MODEL,
                    "prompt": f"{TASK_URL_PREFIX}{task_id}",
                    "seedance_task_id": task_id,
                    "wait_seconds": 0,
                },
            )
            outputs = poll.get("data") or []
            if not outputs:
                sys.exit(f"No data in poll response: {poll}")
            url = outputs[0].get("url")
            status = outputs[0].get("revised_prompt") or ""
            if url and not _is_task_url(url):
                video_url = url
                break
            print(f"[poll]   status={status or 'running'}")

    print("[done]   received video URL from LiteLLM")
    print(f"[done]   url={video_url[:120]}{'…' if len(video_url) > 120 else ''}")

    out_path = SCRIPT_DIR / OUTPUT_FILE

    with httpx.Client(timeout=120) as client:
        dl = client.get(video_url)
        dl.raise_for_status()
    out_path.write_bytes(dl.content)
    size_kb = len(dl.content) / 1024
    print(f"[save]   {out_path}  ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()

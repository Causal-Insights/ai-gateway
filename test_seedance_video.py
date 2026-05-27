#!/usr/bin/env python3
"""
Test Seedance 2.0 video generation through the LiteLLM proxy.

Uses the bounded-wait + polling pattern exposed by the custom handler:

    1. POST /v1/images/generations with the prompt. Within the proxy's
       SEEDANCE_SYNC_WAIT_S window (default 240s) the response may already
       contain the final MP4 URL; otherwise it returns ``seedance-task://<id>``.
    2. If we still have a task URL, POST again with ``seedance_task_id`` until
       the response carries an https://... URL or we hit a timeout.
    3. Download the MP4.

Requires: LITELLM_MASTER_KEY env var.
Optional: LITELLM_API_BASE, SEEDANCE_PROMPT, SEEDANCE_DURATION,
                    SEEDANCE_RESOLUTION, SEEDANCE_RATIO, SEEDANCE_GENERATE_AUDIO,
                    SEEDANCE_WATERMARK, SEEDANCE_MAX_POLL_S
Usage:    python test_seedance_video.py
"""

import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

LITELLM_API_BASE = os.environ.get("LITELLM_API_BASE", "http://localhost:4000").rstrip("/")
MODEL = "seedance-2.0-fast"
PROMPT = os.environ.get(
    "SEEDANCE_PROMPT",
    "A magical forest filled with fireflies.  An tent illuminated from within by warm light. Pan up to see the bright stars and a milky way spiral arm visible from above. "
)
DURATION = int(os.environ.get("SEEDANCE_DURATION", "4"))
RESOLUTION = os.environ.get("SEEDANCE_RESOLUTION", "480p")
RATIO = os.environ.get("SEEDANCE_RATIO", "1:1")
GENERATE_AUDIO = os.environ.get("SEEDANCE_GENERATE_AUDIO", "false").lower() == "true"
WATERMARK = os.environ.get("SEEDANCE_WATERMARK", "false").lower() == "true"
MAX_POLL_S = float(os.environ.get("SEEDANCE_MAX_POLL_S", "1200"))
POLL_INTERVAL_S = 10.0
OUTPUT_FILE = "test_seedance_video.mp4"

TASK_URL_PREFIX = "seedance-task://"


def _is_task_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith(TASK_URL_PREFIX)


def _task_id_from_url(url: str) -> str:
    return url[len(TASK_URL_PREFIX):]


def _post(api_base: str, master_key: str, payload: dict) -> dict:
    # Short HTTP timeout per call — handler bounds wait to SEEDANCE_SYNC_WAIT_S.
    with httpx.Client(timeout=300) as client:
        resp = client.post(
            f"{api_base}/v1/images/generations",
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

    submit_payload = {
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

    data = _post(LITELLM_API_BASE, master_key, submit_payload)
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
                LITELLM_API_BASE,
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

    with httpx.Client(timeout=120) as client:
        dl = client.get(video_url)
        dl.raise_for_status()
    with open(OUTPUT_FILE, "wb") as f:
        f.write(dl.content)
    size_kb = len(dl.content) / 1024
    print(f"[save]   {OUTPUT_FILE}  ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Test BytePlus Seedance 2.0 video generation directly.

Uses the minimum-cost settings: 480p, 1:1, 4s, no audio.

Async flow:
  1. POST /api/v3/contents/generations/tasks -> {"id": "cgt-..."}
  2. GET  /api/v3/contents/generations/tasks/{id} -> poll until status == "succeeded"

Requires: ARK_API_KEY (or BYTEDANCE_API_KEY) env var.
Usage:    python test_seedance_video.py
"""

import os
import sys
import time

import httpx

ARK_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"
MODEL = "dreamina-seedance-2-0-260128"
PROMPT = (
    "A pale, elegant vampire with long blonde hair and aristocratic features sits alone "
    "at a candlelit table in a dimly lit 19th-century Parisian cafe at night. He wears a "
    "dark velvet coat and regards the camera with cold, amused eyes. Slumped in the chair "
    "beside him is a beautiful young woman in a silk gown that leaves little to the imaginatiion and partially exposed.  Her eyes closed, a faint red mark "
    "on her neck. Rain streaks the tall windows. Candle flames flicker. Cinematic, gothic, "
    "moody atmosphere."
)
DURATION = 4        # minimum for Seedance 2.0 — cheapest possible
RESOLUTION = "480p" # lowest available
RATIO = "1:1"       # 640×640 at 480p — smallest pixel count
POLL_INTERVAL = 10  # seconds between status checks
POLL_TIMEOUT = 300  # give up after 5 minutes
OUTPUT_FILE = "test_seedance_vampire.mp4"


def main() -> None:
    api_key = (
        os.environ.get("ARK_API_KEY")
        or os.environ.get("BYTEDANCE_API_KEY")
    )
    if not api_key:
        sys.exit("ERROR: set ARK_API_KEY or BYTEDANCE_API_KEY")

    auth_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # ── 1. Submit generation task ───────────────────────────────────────────
    payload = {
        "model": MODEL,
        "content": [
            {"type": "text", "text": PROMPT},
        ],
        "resolution": RESOLUTION,
        "ratio": RATIO,
        "duration": DURATION,
        "generate_audio": False,
        "watermark": False,
    }

    print(f"[submit] model={MODEL!r}")
    print(f"         {RESOLUTION} {RATIO} {DURATION}s  audio=off")
    print(f"         prompt={PROMPT!r}")

    with httpx.Client(timeout=30) as client:
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

    # ── 2. Poll for completion ──────────────────────────────────────────────
    deadline = time.monotonic() + POLL_TIMEOUT
    with httpx.Client(timeout=15) as client:
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

                # ── 3. Download and save ────────────────────────────────────
                dl = client.get(video_url)
                dl.raise_for_status()
                with open(OUTPUT_FILE, "wb") as f:
                    f.write(dl.content)
                size_kb = len(dl.content) / 1024
                print(f"[save]   {OUTPUT_FILE}  ({size_kb:.1f} KB)")
                return

    sys.exit(f"Timed out after {POLL_TIMEOUT}s waiting for {task_id}")


if __name__ == "__main__":
    main()

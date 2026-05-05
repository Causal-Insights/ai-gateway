#!/usr/bin/env python3
"""
Test xAI grok-imagine-video generation directly against the xAI API.

The xAI video endpoint is async:
  1. POST /v1/videos/generations -> {"request_id": "..."}
  2. GET  /v1/videos/{request_id} -> poll until status == "done"

Requires: GROK_API_KEY (or XAI_API_KEY) env var.
Usage:    python test_grok_video.py
"""

import os
import sys
import time

import httpx

XAI_BASE = "https://api.x.ai/v1"
MODEL = "grok-imagine-video"
PROMPT = "A red ball bouncing once on a white surface, minimal scene"
DURATION = 1        # 1 second — shortest/cheapest possible clip
POLL_INTERVAL = 5   # seconds between status checks
POLL_TIMEOUT = 300  # give up after 5 minutes
OUTPUT_FILE = "test_grok_video.mp4"


def main() -> None:
    api_key = os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY")
    if not api_key:
        sys.exit("ERROR: set GROK_API_KEY or XAI_API_KEY")

    auth_headers = {"Authorization": f"Bearer {api_key}"}

    # ── 1. Submit generation ────────────────────────────────────────────────
    payload = {
        "model": MODEL,
        "prompt": PROMPT,
        "duration": DURATION,
    }
    print(f"[submit] model={MODEL!r} duration={DURATION}s")
    print(f"         prompt={PROMPT!r}")

    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{XAI_BASE}/videos/generations",
            headers={**auth_headers, "Content-Type": "application/json"},
            json=payload,
        )

    if resp.status_code != 200:
        sys.exit(f"Submit failed {resp.status_code}: {resp.text}")

    request_id = resp.json().get("request_id")
    if not request_id:
        sys.exit(f"No request_id in response: {resp.json()}")
    print(f"[submit] request_id={request_id}")

    # ── 2. Poll for completion ──────────────────────────────────────────────
    deadline = time.monotonic() + POLL_TIMEOUT
    with httpx.Client(timeout=15) as client:
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            poll = client.get(
                f"{XAI_BASE}/videos/{request_id}",
                headers=auth_headers,
            )
            if poll.status_code != 200:
                print(f"[poll]   HTTP {poll.status_code}: {poll.text}")
                continue

            data = poll.json()
            status = data.get("status", "unknown")
            progress = data.get("progress", 0)
            print(f"[poll]   status={status} progress={progress}%")

            if status == "failed":
                sys.exit(f"Generation failed: {data}")

            if status == "done":
                video = data["video"]
                video_url = video["url"]
                actual_duration = video.get("duration", "?")
                cost_ticks = data.get("usage", {}).get("cost_in_usd_ticks", 0)
                cost_usd = cost_ticks / 1_000_000_000

                print(f"\n[done]   duration={actual_duration}s  cost=${cost_usd:.4f}")
                print(f"[done]   url={video_url}")

                # ── 3. Download and save ────────────────────────────────────
                dl = client.get(video_url)
                dl.raise_for_status()
                with open(OUTPUT_FILE, "wb") as f:
                    f.write(dl.content)
                size_kb = len(dl.content) / 1024
                print(f"[save]   {OUTPUT_FILE}  ({size_kb:.1f} KB)")
                return

    sys.exit(f"Timed out after {POLL_TIMEOUT}s waiting for {request_id}")


if __name__ == "__main__":
    main()

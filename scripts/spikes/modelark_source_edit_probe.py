"""Paid ModelArk v3 source-edit semantics probe. Does not touch gateway jobs."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx

EVIDENCE = Path(__file__).resolve().parents[2] / "spikes" / "evidence" / "modelark_source_edit_probe.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-video-url", required=True)
    parser.add_argument("--prompt", default="Replace the daytime sky with a clear night sky full of stars.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    api_key = os.environ.get("BYTEDANCE_API_KEY", "").strip()
    base = os.environ.get(
        "SEEDANCE_ARK_BASE_URL",
        "https://ark.ap-southeast-1.bytepluses.com/api/v3",
    ).rstrip("/")
    request = {
        "model": "seedance-2-0-260128",
        "content": [
            {"type": "text", "text": args.prompt},
            {"type": "video_url", "video_url": {"url": args.source_video_url}, "role": "reference_video"},
        ],
    }
    if args.dry_run or not api_key:
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(
            json.dumps(
                {
                    "probe": "modelark_source_edit_probe",
                    "upstream_model": request["model"],
                    "request": request,
                    "response_summary": None,
                    "provider_request_id": None,
                    "cost_usd": None,
                    "mp4": None,
                    "verdict": "pending_human_review",
                },
                indent=2,
            )
            + "\n"
        )
        print("wrote pending evidence; pass --dry-run off with BYTEDANCE_API_KEY to submit")
        return
    started = time.time()
    with httpx.Client(timeout=120) as client:
        response = client.post(
            f"{base}/contents/generations/tasks",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=request,
        )
        response.raise_for_status()
        data = response.json()
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(
        json.dumps(
            {
                "probe": "modelark_source_edit_probe",
                "upstream_model": request["model"],
                "request": request,
                "response_summary": {"status_code": response.status_code, "body": data, "elapsed_s": round(time.time() - started, 2)},
                "provider_request_id": data.get("id") or data.get("task_id"),
                "cost_usd": None,
                "mp4": None,
                "verdict": "pending_human_review",
            },
            indent=2,
        )
        + "\n"
    )
    print(EVIDENCE)


if __name__ == "__main__":
    main()

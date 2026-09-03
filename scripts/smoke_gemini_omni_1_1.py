#!/usr/bin/env python3
"""Paid Gemini Omni Flash 1.1 durable-job smoke runner.

The script prints only job/provider IDs, status, usage, cost, and output path.
It never prints the Gateway key or submitted media bytes.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import time
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import httpx


TERMINAL = {"completed", "failed", "expired", "cancelled"}


def media_entry(media_type: str, role: str, field: str) -> dict[str, str]:
    return {"type": media_type, "role": role, "upload_field": field}


def request_for(args: argparse.Namespace) -> tuple[dict, dict[str, Path]]:
    request = {
        "model": args.model,
        "modality": "video",
        "operation": "generate",
        "prompt": args.prompt,
        "duration_seconds": args.duration,
        "resolution": args.resolution,
        "aspect_ratio": args.aspect_ratio,
        "generate_audio": True,
        "previous_job_id": None,
        "reference_voice_ids": [],
        "media_inputs": [],
        "metadata": {"source": "omni_1_1_paid_smoke", "attempt": args.mode},
    }
    files: dict[str, Path] = {}

    def add_image(field: str, role: str, path: Path | None) -> None:
        if path is None:
            raise SystemExit(f"--{field.replace('_', '-')} is required for {args.mode}")
        request["media_inputs"].append(media_entry("image", role, field))
        files[field] = path

    if args.mode == "first_frame":
        add_image("first_image", "first_frame", args.first_image)
    elif args.mode == "references":
        add_image("first_image", "reference", args.first_image)
        add_image("second_image", "reference", args.second_image)
    elif args.mode == "first_frame_references":
        add_image("first_image", "first_frame", args.first_image)
        add_image("second_image", "reference", args.second_image)
    elif args.mode == "first_last_frame":
        add_image("first_image", "first_frame", args.first_image)
        add_image("second_image", "last_frame", args.second_image)
    elif args.mode in {"source_edit", "source_extend"}:
        if args.source_video is None:
            raise SystemExit(f"--source-video is required for {args.mode}")
        request["operation"] = "edit" if args.mode == "source_edit" else "extend"
        request["aspect_ratio"] = None
        request["media_inputs"].append(media_entry("video", "source", "source_video"))
        files["source_video"] = args.source_video
        if args.mode == "source_extend" and args.first_image:
            add_image("first_image", "reference", args.first_image)
    elif args.mode == "previous_extend":
        if not args.previous_job_id:
            raise SystemExit("--previous-job-id is required for previous_extend")
        request["operation"] = "extend"
        request["aspect_ratio"] = None
        request["previous_job_id"] = args.previous_job_id
    return request, files


def submit(client: httpx.Client, base_url: str, headers: dict[str, str], request: dict, files: dict[str, Path], key: str) -> dict:
    url = f"{base_url}/v1/generation-jobs"
    submit_headers = {**headers, "Idempotency-Key": key}
    if not files:
        response = client.post(url, headers=submit_headers, json=request)
    else:
        with ExitStack() as stack:
            multipart = {
                field: (
                    path.name,
                    stack.enter_context(path.open("rb")),
                    mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                )
                for field, path in files.items()
            }
            multipart["request"] = (None, json.dumps(request), "application/json")
            response = client.post(url, headers=submit_headers, files=multipart)
    if response.status_code != 202:
        raise SystemExit(f"submission failed ({response.status_code}): {response.text[:2000]}")
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=[
        "text", "first_frame", "references", "first_frame_references",
        "first_last_frame", "source_edit", "source_extend", "previous_extend",
    ])
    parser.add_argument("--base-url", default=os.environ.get("GATEWAY_URL", "http://127.0.0.1:4000"))
    parser.add_argument("--model", default="gemini-omni-1.1-flash")
    parser.add_argument("--prompt", default="A paper lantern drifts through a quiet moonlit garden.")
    parser.add_argument("--duration", type=int, default=3)
    parser.add_argument("--resolution", default="360p")
    parser.add_argument("--aspect-ratio", default="16:9")
    parser.add_argument("--first-image", type=Path)
    parser.add_argument("--second-image", type=Path)
    parser.add_argument("--source-video", type=Path)
    parser.add_argument("--previous-job-id")
    parser.add_argument("--idempotency-key", default=f"omni-1-1-smoke-{uuid4().hex}")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    token = os.environ.get("LITELLM_MASTER_KEY") or os.environ.get("GATEWAY_API_KEY")
    if not token:
        raise SystemExit("LITELLM_MASTER_KEY or GATEWAY_API_KEY is required")
    request, files = request_for(args)
    headers = {"Authorization": f"Bearer {token}"}
    cloud_run_identity_token = os.environ.get("CLOUD_RUN_ID_TOKEN")
    if cloud_run_identity_token:
        headers["X-Serverless-Authorization"] = f"Bearer {cloud_run_identity_token}"
    base_url = args.base_url.rstrip("/")
    deadline = time.monotonic() + args.timeout

    with httpx.Client(timeout=httpx.Timeout(180, connect=20), follow_redirects=True) as client:
        job = submit(client, base_url, headers, request, files, args.idempotency_key)
        while job.get("status") not in TERMINAL:
            if time.monotonic() >= deadline:
                raise SystemExit(f"timed out waiting for {job.get('id')}")
            wait_seconds = max(1, min(20, int(job.get("poll_after_ms") or 5000) // 1000))
            time.sleep(wait_seconds)
            response = client.get(f"{base_url}/v1/generation-jobs/{job['id']}", headers=headers)
            response.raise_for_status()
            job = response.json()

        result = {
            key: job.get(key)
            for key in ("id", "model", "status", "provider_request_id", "usage", "cost_usd", "error")
            if job.get(key) is not None
        }
        if job.get("status") == "completed":
            output = args.output or Path("local-tests") / f"omni-1.1-{args.mode}-{job['id']}.mp4"
            output.parent.mkdir(parents=True, exist_ok=True)
            with client.stream("GET", f"{base_url}/v1/generation-jobs/{job['id']}/content", headers=headers) as response:
                response.raise_for_status()
                with output.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
            result["output"] = str(output)
            result["content_bytes"] = output.stat().st_size
        print(json.dumps(result, indent=2, sort_keys=True))
        if job.get("status") != "completed":
            raise SystemExit(1)


if __name__ == "__main__":
    main()

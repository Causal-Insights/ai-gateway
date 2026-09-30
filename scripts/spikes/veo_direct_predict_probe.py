"""Vertex Veo predictLongRunning probe with ADC and GCS output. Does not touch gateway jobs."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx

EVIDENCE = Path(__file__).resolve().parents[2] / "spikes" / "evidence" / "veo_direct_predict_probe.json"


def _headers() -> dict[str, str]:
    import google.auth
    from google.auth.transport.requests import Request as GoogleAuthRequest

    credentials, _project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not credentials.valid or credentials.expired or not credentials.token:
        credentials.refresh(GoogleAuthRequest())
    return {"Authorization": f"Bearer {credentials.token}", "Content-Type": "application/json"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", default="A slow dolly through a sunlit conservatory.")
    parser.add_argument("--model", default="veo-3.1-fast-generate-001")
    parser.add_argument("--aspect-ratio", default="16:9")
    parser.add_argument("--resolution", default="1080p")
    parser.add_argument("--storage-uri", default=os.environ.get("VEO_OUTPUT_GCS_PREFIX", ""))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    location = os.environ.get("VERTEX_LOCATION", "us-central1")
    request = {
        "instances": [{"prompt": args.prompt}],
        "parameters": {
            "aspectRatio": args.aspect_ratio,
            "resolution": args.resolution,
            "sampleCount": 1,
            **({"storageUri": args.storage_uri} if args.storage_uri else {}),
        },
    }
    url = (
        f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}"
        f"/publishers/google/models/{args.model}:predictLongRunning"
    )
    if args.dry_run or not project:
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(
            json.dumps(
                {
                    "probe": "veo_direct_predict_probe",
                    "upstream_model": args.model,
                    "request": {"url": url, "body": request},
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
        print("wrote pending evidence; set GOOGLE_CLOUD_PROJECT and omit --dry-run to submit")
        return
    started = time.time()
    with httpx.Client(timeout=120) as client:
        response = client.post(url, headers=_headers(), json=request)
        response.raise_for_status()
        data = response.json()
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(
        json.dumps(
            {
                "probe": "veo_direct_predict_probe",
                "upstream_model": args.model,
                "request": {"url": url, "body": request},
                "response_summary": {"status_code": response.status_code, "body": data, "elapsed_s": round(time.time() - started, 2)},
                "provider_request_id": data.get("name"),
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

"""Replay frozen V1 generation-job bodies through _hash_request and write goldens."""

from __future__ import annotations

import json
from pathlib import Path

from generation_job_models import GenerationJobCreate
from generation_job_routes import _hash_request

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "generation_jobs_v1"
EVIDENCE = ROOT / "spikes" / "evidence" / "capture_v1_hash_corpus.json"

BODIES = {
    "grok_video_15_text.json": {
        "model": "grok-video-1.5",
        "prompt": "a lantern in rain",
        "duration_seconds": 6,
        "resolution": "720p",
        "aspect_ratio": "16:9",
        "generate_audio": True,
    },
    "seedance_20_text.json": {
        "model": "seedance-2.0",
        "prompt": "a conservatory dolly",
        "duration_seconds": 8,
        "resolution": "720p",
        "aspect_ratio": "16:9",
        "generate_audio": True,
    },
    "veo_31_fast_text.json": {
        "model": "veo-3.1-fast",
        "prompt": "sunlit conservatory",
        "duration_seconds": 8,
        "resolution": "720p",
        "aspect_ratio": "16:9",
        "generate_audio": True,
    },
    "omni_edit_source.json": {
        "model": "gemini-omni-flash",
        "operation": "edit",
        "prompt": "replace the sky",
        "resolution": "720p",
        "media_inputs": [{"type": "video", "role": "source", "url": "https://assets.example/source.mp4"}],
    },
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    written = []
    for name, body in BODIES.items():
        payload = GenerationJobCreate.model_validate(body)
        request_hash = _hash_request(payload, {})
        record = {"body": body, "request_hash": request_hash}
        (OUT / name).write_text(json.dumps(record, indent=2) + "\n")
        written.append({"fixture": name, "request_hash": request_hash, "model": body["model"]})
    EVIDENCE.write_text(
        json.dumps(
            {
                "probe": "capture_v1_hash_corpus",
                "upstream_model": None,
                "request": {"fixtures": list(BODIES)},
                "response_summary": written,
                "provider_request_id": None,
                "cost_usd": 0,
                "mp4": None,
                "verdict": "V1 hashes captured from frozen GenerationJobCreate bodies; no provider call.",
            },
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {len(written)} fixtures to {OUT}")


if __name__ == "__main__":
    main()

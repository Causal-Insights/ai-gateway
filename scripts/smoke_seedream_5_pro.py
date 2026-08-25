#!/usr/bin/env python3
"""Guarded paid localhost smoke for Seedream 5.0 Pro.

This script intentionally refuses non-loopback Gateway URLs and requires an
explicit ``--confirm-paid`` acknowledgement before it submits a provider job.
"""

from __future__ import annotations

import argparse
import base64
import os
import struct
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv


load_dotenv()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-paid", action="store_true")
    parser.add_argument(
        "--api-base",
        default="http://127.0.0.1:4000",
        help="Explicit local Gateway URL. Environment proxy URLs are intentionally ignored.",
    )
    parser.add_argument("--model", default="seedream-5.0-pro")
    parser.add_argument("--size", default="1424x800")
    parser.add_argument(
        "--prompt",
        default=(
            "A clean editorial photograph of a red canoe beside a quiet alpine lake, "
            "soft morning fog, realistic natural light, no text"
        ),
    )
    parser.add_argument(
        "--output",
        default="local-tests/seedream-5.0-pro-paid-smoke.png",
    )
    return parser.parse_args()


def _require_loopback(api_base: str) -> None:
    host = (urlparse(api_base).hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        sys.exit(f"Refusing paid smoke against non-loopback Gateway host: {host or api_base}")


def _generation_url(api_base: str) -> str:
    base = api_base.rstrip("/")
    return f"{base}/images/generations" if base.endswith("/v1") else f"{base}/v1/images/generations"


def _decode_image(result: dict, timeout_s: float = 180.0) -> tuple[bytes, str]:
    outputs = result.get("data") or []
    if not outputs or not isinstance(outputs[0], dict):
        sys.exit(f"Gateway response has no image data: {result}")
    first = outputs[0]
    if isinstance(first.get("b64_json"), str):
        return base64.b64decode(first["b64_json"]), "image/png"
    image_url = first.get("url")
    if not isinstance(image_url, str) or not image_url:
        sys.exit(f"Gateway response has no image URL or b64_json: {result}")
    with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
        response = client.get(image_url)
        response.raise_for_status()
    return response.content, response.headers.get("content-type", "").split(";", 1)[0]


def _dimensions(raw: bytes) -> tuple[str, int, int]:
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        width, height = struct.unpack(">II", raw[16:24])
        return "image/png", width, height
    if raw.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 < len(raw):
            if raw[offset] != 0xFF:
                offset += 1
                continue
            marker = raw[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9}:
                continue
            if offset + 2 > len(raw):
                break
            length = int.from_bytes(raw[offset : offset + 2], "big")
            if marker in range(0xC0, 0xC4):
                height = int.from_bytes(raw[offset + 3 : offset + 5], "big")
                width = int.from_bytes(raw[offset + 5 : offset + 7], "big")
                return "image/jpeg", width, height
            offset += length
    sys.exit("Downloaded payload is not a decodable PNG or JPEG image")


def main() -> None:
    args = _parse_args()
    if not args.confirm_paid:
        sys.exit("Paid smoke not confirmed. Re-run with --confirm-paid.")
    _require_loopback(args.api_base)
    master_key = os.environ.get("LITELLM_MASTER_KEY", "").strip()
    if not master_key:
        sys.exit("LITELLM_MASTER_KEY is required")

    payload = {
        "model": args.model,
        "prompt": args.prompt,
        "size": args.size,
        "n": 1,
        "output_format": "png",
        "response_format": "url",
        "watermark": False,
    }
    with httpx.Client(timeout=300.0) as client:
        response = client.post(
            _generation_url(args.api_base),
            headers={"Authorization": f"Bearer {master_key}"},
            json=payload,
        )
    if response.status_code != 200:
        sys.exit(f"Gateway request failed {response.status_code}: {response.text[:2000]}")

    raw, response_mime = _decode_image(response.json())
    detected_mime, width, height = _dimensions(raw)
    requested_width, requested_height = (int(value) for value in args.size.lower().split("x", 1))
    if (width, height) != (requested_width, requested_height):
        sys.exit(
            f"Provider returned {width}x{height}; expected exact requested size "
            f"{requested_width}x{requested_height}"
        )

    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    print(
        f"PASS model={args.model} size={width}x{height} mime={detected_mime} "
        f"response_mime={response_mime or 'unknown'} bytes={len(raw)} output={output}"
    )


if __name__ == "__main__":
    main()

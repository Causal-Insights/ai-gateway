"""Authenticated health probe used by Docker Compose and container platforms."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> int:
    master_key = os.environ.get("LITELLM_MASTER_KEY", "").strip()
    if not master_key:
        print("LITELLM_MASTER_KEY is not configured")
        return 1

    port = os.environ.get("PORT", "8080")
    request = Request(
        f"http://127.0.0.1:{port}/health/liveliness",
        headers={"Authorization": f"Bearer {master_key}"},
    )
    try:
        with urlopen(request, timeout=5) as response:
            body = response.read()
            if response.status != 200:
                print(f"liveliness returned HTTP {response.status}")
                return 1
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"liveliness probe failed: {exc}")
        return 1

    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        print("liveliness returned malformed JSON")
        return 1
    if payload != {"status": "healthy"} and payload != "I'm alive!":
        print(f"unexpected liveliness response: {payload!r}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

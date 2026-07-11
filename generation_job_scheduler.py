"""Cloud Tasks scheduling for short, one-status-check polling invocations."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx


POLL_DELAYS_SECONDS = (5, 10, 20)
_token: Optional[str] = None
_token_expires_at = 0.0
_token_lock = asyncio.Lock()


def next_poll_time(attempt: int, *, now: Optional[datetime] = None) -> datetime:
    base = POLL_DELAYS_SECONDS[min(max(0, attempt), len(POLL_DELAYS_SECONDS) - 1)]
    jittered = base * random.uniform(0.8, 1.2)
    return (now or datetime.now(timezone.utc)) + timedelta(seconds=jittered)


def _queue_config() -> Optional[dict[str, str]]:
    project = os.environ.get("GENERATION_POLL_QUEUE_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GENERATION_POLL_QUEUE_LOCATION") or os.environ.get("GOOGLE_CLOUD_LOCATION")
    queue = os.environ.get("GENERATION_POLL_QUEUE_NAME", "ai-generation-polls")
    target = os.environ.get("GENERATION_POLL_TARGET_URL")
    service_account = os.environ.get("GENERATION_POLL_SERVICE_ACCOUNT_EMAIL")
    if not all((project, location, queue, target, service_account)):
        return None
    return {
        "project": project,
        "location": location,
        "queue": queue,
        "target": target.rstrip("/"),
        "service_account": service_account,
        "audience": os.environ.get("GENERATION_POLL_AUDIENCE") or target.rstrip("/"),
    }


async def _access_token() -> str:
    global _token, _token_expires_at
    if _token and time.monotonic() < _token_expires_at:
        return _token
    async with _token_lock:
        if _token and time.monotonic() < _token_expires_at:
            return _token
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                headers={"Metadata-Flavor": "Google"},
            )
            response.raise_for_status()
            data = response.json()
        _token = str(data["access_token"])
        _token_expires_at = time.monotonic() + max(30, int(data.get("expires_in", 300)) - 60)
        return _token


async def enqueue_poll(job_id: str, when: datetime) -> bool:
    """Enqueue one idempotent poll task; false means queueing is not configured."""
    config = _queue_config()
    if config is None:
        return False
    when = when.astimezone(timezone.utc)
    slot = int(when.timestamp())
    digest = hashlib.sha256(f"{job_id}:{slot}".encode()).hexdigest()[:24]
    parent = f"projects/{config['project']}/locations/{config['location']}/queues/{config['queue']}"
    task_name = f"{parent}/tasks/job-{digest}"
    body = base64.b64encode(b"{}").decode("ascii")
    headers = {"Content-Type": "application/json"}
    internal_secret = os.environ.get("GENERATION_INTERNAL_SECRET")
    if internal_secret:
        headers["X-Gateway-Internal-Secret"] = internal_secret
    payload = {
        "task": {
            "name": task_name,
            "scheduleTime": when.isoformat().replace("+00:00", "Z"),
            "httpRequest": {
                "httpMethod": "POST",
                "url": f"{config['target']}/internal/generation-jobs/{job_id}/poll",
                "headers": headers,
                "body": body,
                "oidcToken": {
                    "serviceAccountEmail": config["service_account"],
                    "audience": config["audience"],
                },
            },
        }
    }
    token = await _access_token()
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"https://cloudtasks.googleapis.com/v2/{parent}/tasks",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    if response.status_code == 409:
        return True
    response.raise_for_status()
    return True


"""Cloud Tasks scheduling for short, one-status-check polling invocations."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx


POLL_DELAYS_SECONDS = (5, 10, 20)
logger = logging.getLogger("ai_gateway.generation_jobs.scheduler")
_token: Optional[str] = None
_token_expires_at = 0.0
_token_lock = asyncio.Lock()
_local_tasks: set[asyncio.Task[None]] = set()
_local_reconciler: Optional[asyncio.Task[None]] = None


def _local_polling_enabled() -> bool:
    return os.environ.get("GENERATION_LOCAL_POLLING", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _local_target() -> str:
    return os.environ.get(
        "GENERATION_LOCAL_POLL_TARGET_URL",
        f"http://127.0.0.1:{os.environ.get('PORT', '8080')}",
    ).rstrip("/")


def _internal_headers() -> dict[str, str]:
    secret = os.environ.get("GENERATION_INTERNAL_SECRET")
    return {"X-Gateway-Internal-Secret": secret} if secret else {}


async def _post_local(path: str) -> None:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(f"{_local_target()}{path}", headers=_internal_headers())
        response.raise_for_status()


def _track_local_task(task: asyncio.Task[None]) -> None:
    _local_tasks.add(task)
    task.add_done_callback(_local_tasks.discard)


async def _run_local_poll(job_id: str, when: datetime) -> None:
    delay = max(0.0, (when.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())
    await asyncio.sleep(delay)
    try:
        await _post_local(f"/internal/generation-jobs/{job_id}/poll")
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("local_generation_poll_failed", extra={"generation_job_id": job_id})


def _schedule_local_poll(job_id: str, when: datetime) -> None:
    _track_local_task(
        asyncio.create_task(
            _run_local_poll(job_id, when), name=f"generation-local-poll-{job_id}"
        )
    )


async def _local_reconcile_loop() -> None:
    # Recover due jobs after a local container restart and retry a poll whose
    # loopback request failed. Production recovery remains Cloud Scheduler/Tasks.
    await asyncio.sleep(2)
    while True:
        try:
            await _post_local("/internal/generation-jobs/reconcile")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("local_generation_reconcile_failed")
        await asyncio.sleep(10)


async def start_local_scheduler() -> None:
    global _local_reconciler
    if not _local_polling_enabled() or (_local_reconciler and not _local_reconciler.done()):
        return
    _local_reconciler = asyncio.create_task(
        _local_reconcile_loop(), name="generation-local-reconciler"
    )


async def stop_local_scheduler() -> None:
    global _local_reconciler
    tasks = list(_local_tasks)
    if _local_reconciler:
        tasks.append(_local_reconciler)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _local_tasks.clear()
    _local_reconciler = None


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
    if _local_polling_enabled():
        _schedule_local_poll(job_id, when)
        return True
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

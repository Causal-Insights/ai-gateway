import asyncio
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from . import providers
from .internal import collect_internal
from .model import NotConfigured, PROVIDERS, months_before, safe_error

LOG = logging.getLogger("dashboard.collector")
STREAMS = {
    "openai": ["costs", "completions", "embeddings", "moderations", "images", "audio_speeches",
               "audio_transcriptions", "vector_stores", "code_interpreter_sessions", "web_searches", "file_searches"],
    "xai": ["costs", "balance"], "byteplus": ["costs", "usage"],
    "elevenlabs": ["usage", "subscription"], "google": ["costs"], "minimax": ["usage"],
}


async def collect_range(store, env, start, end, selected=None):
    selected = selected or list(PROVIDERS)
    results = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False) as client:
        for provider in selected:
            for stream in STREAMS[provider]:
                key = f"provider:{provider}:{stream}"
                try:
                    if provider == "google":
                        batch = await asyncio.to_thread(providers.google_billing, env, start, end)
                    else:
                        adapter = getattr(providers, provider)
                        provider_end = max(end, datetime.now(ZoneInfo("Asia/Shanghai")).date()) if provider == "byteplus" and end == datetime.now(timezone.utc).date() else end
                        batch = await adapter(client, env, start, provider_end, stream)
                    await asyncio.to_thread(store.save, batch)
                    results.append({"key": key, "ok": True, "rows": len(batch.rows)})
                except Exception as exc:
                    message = safe_error(exc)
                    await asyncio.to_thread(store.failure, key, message, not isinstance(exc, NotConfigured))
                    results.append({"key": key, "ok": False, "message": message})
                LOG.info("source_refresh %s %s", key, "updated" if results[-1]["ok"] else "unavailable")
    for source in ("gateway", "litellm", "magiclens"):
        try:
            batches = await collect_internal(env, source, start, end)
            for batch in batches:
                if batch.provider in selected:
                    await asyncio.to_thread(store.save, batch)
            results.append({"key": source, "ok": True})
        except Exception as exc:
            for provider in selected:
                await asyncio.to_thread(store.failure, f"{source}:{provider}:costs", safe_error(exc), not isinstance(exc, NotConfigured))
            results.append({"key": source, "ok": False, "message": safe_error(exc)})
    return results


async def run(store, env=None, start=None, end=None, selected=None):
    env = dict(os.environ) if env is None else env
    owner = str(uuid.uuid4())
    if not await asyncio.to_thread(store.claim, owner):
        return {"state": "already_running"}
    today = datetime.now(timezone.utc).date()
    explicit = start is not None
    cutoff = months_before(today, 13)
    try:
        if explicit:
            ranges = [(start, end or today)]
        else:
            daily = await asyncio.to_thread(store.meta, "daily_refresh")
            first = not await asyncio.to_thread(store.meta, "initialized")
            recent = today.replace(day=1) if first else months_before(today.replace(day=1), 1) if daily != str(today) else today - timedelta(days=6)
            ranges = [(recent, today)]
            # Backfill one month per hourly run. Failed windows stay eligible for a
            # later explicit retry; they never block current data or other sources.
            cursor = await asyncio.to_thread(store.meta, "backfill_before")
            older_end = date.fromisoformat(cursor) - timedelta(days=1) if cursor else recent - timedelta(days=1)
            if older_end >= cutoff:
                ranges.append((max(cutoff, older_end.replace(day=1)), older_end))
        results = []
        # Cloud Run task timeout is 20 minutes; leave time to release the lease.
        async with asyncio.timeout(19 * 60):
            for index, (a, b) in enumerate(ranges):
                # A bounded CLI range is split into months to respect API limits.
                cursor = a
                while cursor <= b:
                    next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
                    chunk_end = min(b, next_month - timedelta(days=1))
                    results.extend(await collect_range(store, env, cursor, chunk_end, selected))
                    cursor = chunk_end + timedelta(days=1)
                if not explicit and index == 0:
                    await asyncio.to_thread(store.meta, "initialized", True)
                    await asyncio.to_thread(store.meta, "daily_refresh", str(today))
                    if not await asyncio.to_thread(store.meta, "backfill_before"):
                        await asyncio.to_thread(store.meta, "backfill_before", str(a))
                elif not explicit:
                    await asyncio.to_thread(store.meta, "backfill_before", str(a))
        await asyncio.to_thread(store.prune, cutoff)
        return {"state": "complete", "results": results}
    finally:
        await asyncio.to_thread(store.finish, owner)

"""PostgreSQL persistence for durable generation jobs."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

from generation_job_models import ProviderStatus, TERMINAL_STATUSES


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _row(value: Any) -> Optional[dict]:
    if not value:
        return None
    result = dict(value)
    for field in ("owner_context", "request_metadata", "usage"):
        if isinstance(result.get(field), str):
            try:
                result[field] = json.loads(result[field])
            except json.JSONDecodeError:
                result[field] = {} if field != "usage" else None
    return result


def _database_url() -> str:
    raw = (os.environ.get("GATEWAY_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not raw:
        raise RuntimeError("GATEWAY_DATABASE_URL or DATABASE_URL is required for generation jobs")
    parts = urlsplit(raw)
    ignored = {"connection_limit", "pool_timeout", "schema", "pgbouncer"}
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k not in ignored])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


class GenerationJobRepository:
    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None
        self._init_lock = asyncio.Lock()

    async def pool(self) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool
        async with self._init_lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    dsn=_database_url(),
                    min_size=1,
                    max_size=max(1, int(os.environ.get("GENERATION_DB_POOL_SIZE", "5"))),
                    command_timeout=30,
                )
                await self._migrate()
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _migrate(self) -> None:
        assert self._pool is not None
        migration = Path(__file__).with_name("migrations").joinpath("001_generation_jobs.sql").read_text()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("select pg_advisory_xact_lock($1)", 7_451_913_701)
                await conn.execute(migration)

    async def create_or_get(
        self,
        *,
        job_id: str,
        owner_key_hash: str,
        owner_context: dict,
        idempotency_key: str,
        request_hash: str,
        modality: str,
        model: str,
        provider: str,
        request_metadata: dict,
        deadline_at: datetime,
        callback_token_hash: Optional[str],
    ) -> tuple[dict, bool, bool]:
        pool = await self.pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    insert into gateway_generation_jobs (
                      id, owner_key_hash, owner_context, idempotency_key, request_hash,
                      modality, model, provider, status, request_metadata, deadline_at,
                      callback_token_hash
                    ) values ($1,$2,$3::jsonb,$4,$5,$6,$7,$8,'submitting',$9::jsonb,$10,$11)
                    on conflict (owner_key_hash, idempotency_key) do nothing
                    returning *
                    """,
                    job_id,
                    owner_key_hash,
                    _json(owner_context),
                    idempotency_key,
                    request_hash,
                    modality,
                    model,
                    provider,
                    _json(request_metadata),
                    deadline_at,
                    callback_token_hash,
                )
                if row:
                    return _row(row) or {}, True, False
                existing = await conn.fetchrow(
                    """
                    select * from gateway_generation_jobs
                    where owner_key_hash=$1 and idempotency_key=$2
                    """,
                    owner_key_hash,
                    idempotency_key,
                )
                result = _row(existing) or {}
                return result, False, result.get("request_hash") != request_hash

    async def get(self, job_id: str, owner_key_hash: Optional[str] = None) -> Optional[dict]:
        pool = await self.pool()
        if owner_key_hash is None:
            row = await pool.fetchrow("select * from gateway_generation_jobs where id=$1", job_id)
        else:
            row = await pool.fetchrow(
                "select * from gateway_generation_jobs where id=$1 and owner_key_hash=$2",
                job_id,
                owner_key_hash,
            )
        return _row(row)

    async def mark_submitted(
        self,
        job_id: str,
        *,
        provider_request_id: str,
        provider_status: str,
        progress: Optional[float],
        request_metadata: dict,
        next_poll_at: datetime,
    ) -> dict:
        pool = await self.pool()
        row = await pool.fetchrow(
            """
            update gateway_generation_jobs
            set status='queued', provider_request_id=$2, provider_status=$3, progress=$4,
                request_metadata=request_metadata || $5::jsonb,
                submitted_at=now(), next_poll_at=$6, updated_at=now()
            where id=$1 and status='submitting'
            returning *
            """,
            job_id,
            provider_request_id,
            provider_status,
            progress,
            _json(request_metadata),
            next_poll_at,
        )
        if not row:
            raise RuntimeError(f"generation job {job_id} could not be marked submitted")
        return _row(row) or {}

    async def mark_submission_failed(self, job_id: str, *, code: str, message: str) -> dict:
        pool = await self.pool()
        row = await pool.fetchrow(
            """
            update gateway_generation_jobs
            set status='failed', error_code=$2, error_message=$3, error_retryable=false,
                completed_at=now(), updated_at=now(), next_poll_at=null
            where id=$1 and status='submitting'
            returning *
            """,
            job_id,
            code,
            message[:4000],
        )
        return _row(row) or (await self.get(job_id) or {})

    async def apply_provider_status(self, job_id: str, status: ProviderStatus) -> dict:
        pool = await self.pool()
        terminal = status.status in TERMINAL_STATUSES
        row = await pool.fetchrow(
            """
            update gateway_generation_jobs
            set status=$2, provider_status=$3, progress=$4, result_url=coalesce($5,result_url),
                result_mime_type=coalesce($6,result_mime_type), error_code=$7,
                error_message=$8, error_retryable=$9, usage=coalesce($10::jsonb,usage),
                response_cost_usd=coalesce($11,response_cost_usd), poll_attempts=poll_attempts+1,
                consecutive_poll_errors=0, last_polled_at=now(),
                next_poll_at=case when $12 then null else next_poll_at end,
                completed_at=case when $12 then coalesce(completed_at,now()) else completed_at end,
                updated_at=now()
            where id=$1 and status not in ('completed','failed','expired','cancelled')
            returning *
            """,
            job_id,
            status.status,
            status.provider_status,
            status.progress,
            status.result_url,
            status.result_mime_type,
            status.error_code,
            status.error_message,
            status.error_retryable,
            _json(status.usage) if status.usage is not None else None,
            status.cost_usd,
            terminal,
        )
        return _row(row) or (await self.get(job_id) or {})

    async def mark_expired(self, job_id: str) -> dict:
        pool = await self.pool()
        row = await pool.fetchrow(
            """
            update gateway_generation_jobs
            set status='expired', provider_status=coalesce(provider_status,'unknown'),
                error_code='GATEWAY_JOB_DEADLINE_EXCEEDED',
                error_message='Generation did not reach a terminal provider state before the gateway deadline.',
                error_retryable=false, next_poll_at=null, completed_at=now(), updated_at=now()
            where id=$1 and status not in ('completed','failed','expired','cancelled')
            returning *
            """,
            job_id,
        )
        return _row(row) or (await self.get(job_id) or {})

    async def record_poll_error(self, job_id: str, *, message: str, next_poll_at: datetime) -> dict:
        pool = await self.pool()
        row = await pool.fetchrow(
            """
            update gateway_generation_jobs
            set poll_attempts=poll_attempts+1, consecutive_poll_errors=consecutive_poll_errors+1,
                last_polled_at=now(), next_poll_at=$2,
                error_code='STATUS_RETRIEVAL_TRANSIENT', error_message=$3,
                error_retryable=true, updated_at=now()
            where id=$1 and status in ('queued','in_progress')
            returning *
            """,
            job_id,
            next_poll_at,
            message[:4000],
        )
        return _row(row) or (await self.get(job_id) or {})

    async def schedule_next(self, job_id: str, when: datetime) -> None:
        pool = await self.pool()
        await pool.execute(
            """
            update gateway_generation_jobs set next_poll_at=$2, updated_at=now()
            where id=$1 and status in ('queued','in_progress')
            """,
            job_id,
            when,
        )

    async def refresh_result_url(self, job_id: str, url: str, mime_type: Optional[str]) -> dict:
        pool = await self.pool()
        row = await pool.fetchrow(
            """
            update gateway_generation_jobs
            set result_url=$2, result_mime_type=coalesce($3,result_mime_type), updated_at=now()
            where id=$1 and status='completed'
            returning *
            """,
            job_id,
            url,
            mime_type,
        )
        return _row(row) or (await self.get(job_id) or {})

    async def due_jobs(self, limit: int = 100) -> list[dict]:
        pool = await self.pool()
        rows = await pool.fetch(
            """
            select * from gateway_generation_jobs
            where status in ('submitting','queued','in_progress') and next_poll_at <= now()
            order by next_poll_at asc limit $1
            """,
            limit,
        )
        return [_row(row) or {} for row in rows]

    async def jobs_needing_spend(self, limit: int = 100) -> list[dict]:
        pool = await self.pool()
        rows = await pool.fetch(
            """
            select * from gateway_generation_jobs
            where status='completed' and response_cost_usd is not null and spend_logged_at is null
            order by completed_at asc limit $1
            """,
            limit,
        )
        return [_row(row) or {} for row in rows]

    async def record_callback(self, job_id: str, token_hash: str) -> Optional[dict]:
        pool = await self.pool()
        row = await pool.fetchrow(
            """
            update gateway_generation_jobs
            set callback_received_at=coalesce(callback_received_at,now()),
                next_poll_at=case
                  when status in ('completed','failed','expired','cancelled') then null
                  else now()
                end,
                updated_at=now()
            where id=$1 and callback_token_hash=$2
            returning *
            """,
            job_id,
            token_hash,
        )
        return _row(row)

    async def log_spend_once(
        self, job_id: str, callback: Callable[[dict], Awaitable[None]]
    ) -> bool:
        pool = await self.pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "select * from gateway_generation_jobs where id=$1 for update", job_id
                )
                if not row or row["spend_logged_at"] is not None:
                    return False
                await callback(_row(row) or {})
                await conn.execute(
                    "update gateway_generation_jobs set spend_logged_at=now(), updated_at=now() where id=$1",
                    job_id,
                )
                return True

    async def cleanup(self, retention_days: int = 30) -> int:
        pool = await self.pool()
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, retention_days))
        result = await pool.execute(
            """
            delete from gateway_generation_jobs
            where status in ('completed','failed','expired','cancelled')
              and completed_at < $1
            """,
            cutoff,
        )
        return int(result.rsplit(" ", 1)[-1])


repository = GenerationJobRepository()

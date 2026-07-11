import os
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from generation_job_models import ProviderStatus
from generation_job_repository import GenerationJobRepository


@unittest.skipUnless(os.environ.get("RUN_GENERATION_DB_TESTS") == "1", "requires local PostgreSQL")
class GenerationJobRepositoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repository = GenerationJobRepository()
        self.job_id = f"test_{uuid4().hex}"
        self.owner = uuid4().hex
        self.idempotency = uuid4().hex

    async def asyncTearDown(self):
        pool = await self.repository.pool()
        await pool.execute("delete from gateway_generation_jobs where owner_key_hash=$1", self.owner)
        await self.repository.close()

    async def test_concurrent_idempotent_creation_returns_one_job(self):
        base = dict(
            owner_key_hash=self.owner,
            owner_context={},
            idempotency_key=self.idempotency,
            request_hash="same-hash",
            modality="video",
            model="seedance-2.0",
            provider="byteplus",
            request_metadata={},
            deadline_at=datetime.now(timezone.utc) + timedelta(hours=2),
            callback_token_hash=None,
        )
        import asyncio

        first, second = await asyncio.gather(
            self.repository.create_or_get(job_id=self.job_id, **base),
            self.repository.create_or_get(job_id=f"test_{uuid4().hex}", **base),
        )
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(sum(1 for result in (first, second) if result[1]), 1)
        self.assertFalse(first[2])
        self.assertFalse(second[2])

    async def test_idempotency_terminal_compare_and_set_and_spend_guard(self):
        arguments = dict(
            job_id=self.job_id,
            owner_key_hash=self.owner,
            owner_context={"user_id": "integration-test"},
            idempotency_key=self.idempotency,
            request_hash="hash-a",
            modality="video",
            model="grok-video",
            provider="xai",
            request_metadata={},
            deadline_at=datetime.now(timezone.utc) + timedelta(hours=2),
            callback_token_hash=None,
        )
        row, created, conflict = await self.repository.create_or_get(**arguments)
        self.assertTrue(created)
        self.assertFalse(conflict)
        self.assertIsNone(await self.repository.get(self.job_id, "another-owner"))

        replay, created, conflict = await self.repository.create_or_get(**arguments)
        self.assertFalse(created)
        self.assertFalse(conflict)
        self.assertEqual(replay["id"], self.job_id)

        conflicting = {**arguments, "request_hash": "hash-b"}
        _row, created, conflict = await self.repository.create_or_get(**conflicting)
        self.assertFalse(created)
        self.assertTrue(conflict)

        await self.repository.mark_submitted(
            self.job_id,
            provider_request_id="provider-" + uuid4().hex,
            provider_status="pending",
            progress=0,
            request_metadata={},
            next_poll_at=datetime.now(timezone.utc),
        )
        completed = await self.repository.apply_provider_status(
            self.job_id,
            ProviderStatus(
                status="completed",
                provider_status="done",
                progress=100,
                result_url="https://example.com/video.mp4",
                cost_usd=1.25,
            ),
        )
        self.assertEqual(completed["status"], "completed")
        after_race = await self.repository.apply_provider_status(
            self.job_id,
            ProviderStatus(status="failed", provider_status="failed", error_code="LATE"),
        )
        self.assertEqual(after_race["status"], "completed")

        calls = 0

        async def log_once(_job):
            nonlocal calls
            calls += 1

        self.assertTrue(await self.repository.log_spend_once(self.job_id, log_once))
        self.assertFalse(await self.repository.log_spend_once(self.job_id, log_once))
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()

"""In-memory job store.

Deliberately not Postgres/Redis: this service's job state is small, short-lived
and single-process. It sits behind the JobRepository port, so swapping in a
durable store later is an adapter change and nothing else.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict

from app.domain.models import Job


class InMemoryJobRepository:
    def __init__(self, max_jobs: int = 1000):
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._by_key: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._max = max_jobs

    async def create(self, job: Job) -> Job:
        async with self._lock:
            self._jobs[job.job_id] = job
            self._by_key[job.idempotency_key] = job.job_id
            while len(self._jobs) > self._max:                # bounded memory
                old_id, old = self._jobs.popitem(last=False)
                self._by_key.pop(old.idempotency_key, None)
            return job

    async def get(self, job_id: str) -> Job | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def update(self, job: Job) -> Job:
        async with self._lock:
            self._jobs[job.job_id] = job
            return job

    async def find_by_idempotency_key(self, key: str) -> Job | None:
        async with self._lock:
            job_id = self._by_key.get(key)
            return self._jobs.get(job_id) if job_id else None

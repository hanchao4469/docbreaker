"""Shared, cancellation-safe limits with public operational counters."""

import asyncio


class ConcurrencyLimiter:
    def __init__(self, limit: int):
        self.limit = limit
        self.active = self.waiting = self.peak = 0
        self._semaphore = asyncio.Semaphore(limit)

    async def __aenter__(self):
        self.waiting += 1
        try:
            await self._semaphore.acquire()
        finally:
            self.waiting -= 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        return self

    async def __aexit__(self, *unused):
        self.active -= 1
        self._semaphore.release()

"""Return allocator high-water memory after both PDF and web work becomes idle."""
import asyncio
import ctypes
import gc
import sys
import time
from contextlib import suppress
from pathlib import Path

from .events import event


def rss_bytes():
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


def reclaim_memory():
    """GC clears unreachable cycles; glibc trim returns free heap pages, not live objects."""
    before = rss_bytes()
    collected = gc.collect()
    trimmed = False
    if sys.platform == 'linux':
        try:
            trim = ctypes.CDLL(None).malloc_trim
            trim.argtypes = [ctypes.c_size_t]
            trim.restype = ctypes.c_int
            trimmed = bool(trim(0))
        except (AttributeError, OSError):
            pass  # Other allocators/platforms still get ordinary GC.
    return {'before_bytes': before, 'after_bytes': rss_bytes(),
            'collected_objects': collected, 'allocator_trimmed': trimmed}


class IdleMemoryReclaimer:
    def __init__(self, idle, *, grace=30, cooldown=60, threshold=256 * 1024**2):
        self.idle = idle
        self.grace, self.cooldown, self.threshold = grace, cooldown, threshold
        self.idle_since = None
        self.last_reclaim = None
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.run(), name='idle-memory-reclaimer')

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def tick(self):
        now = time.monotonic()
        if not self.idle():
            self.idle_since = None
            return
        if self.idle_since is None:
            self.idle_since = now
        if now - self.idle_since < self.grace:
            return
        if self.last_reclaim is not None and now - self.last_reclaim < self.cooldown:
            return
        rss = rss_bytes()
        if rss is None or rss < self.threshold:
            return
        self.last_reclaim = now
        # ctypes/glibc trimming is thread-safe; do not block the API event loop.
        # New work can start while this finishes; no live allocation is freed.
        result = await asyncio.to_thread(reclaim_memory)
        event('idle_memory_reclaimed', **result)

    async def run(self):
        while True:
            await asyncio.sleep(10)
            try:
                await self.tick()
            except Exception as exc:
                event('memory_reclaim_failed', error_type=type(exc).__name__)

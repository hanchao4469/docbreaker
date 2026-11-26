import unittest
from unittest.mock import patch
from types import SimpleNamespace

from docbreaker.memory import IdleMemoryReclaimer


class IdleMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_tasks_reset_grace_and_reclamation_is_rate_limited(self):
        idle = False
        now = 0
        reclaimer = IdleMemoryReclaimer(lambda: idle)
        with patch('docbreaker.memory.time', SimpleNamespace(monotonic=lambda: now)), \
             patch('docbreaker.memory.rss_bytes', return_value=1024**3), \
             patch('docbreaker.memory.reclaim_memory', return_value={'before_bytes': 1024**3, 'after_bytes': 64*1024**2}) as reclaim:
            await reclaimer.tick()
            idle = True
            now = 10
            await reclaimer.tick()
            now = 39
            await reclaimer.tick()
            reclaim.assert_not_called()
            idle = False
            now = 40
            await reclaimer.tick()
            idle = True
            now = 50
            await reclaimer.tick()
            now = 80
            await reclaimer.tick()
            self.assertEqual(reclaim.call_count, 1)
            now = 139
            await reclaimer.tick()
            self.assertEqual(reclaim.call_count, 1)
            now = 140
            await reclaimer.tick()
            self.assertEqual(reclaim.call_count, 2)

    async def test_small_process_is_not_forced_to_collect(self):
        reclaimer = IdleMemoryReclaimer(lambda: True, grace=0)
        with patch('docbreaker.memory.rss_bytes', return_value=55*1024**2), \
             patch('docbreaker.memory.reclaim_memory') as reclaim:
            await reclaimer.tick()
            reclaim.assert_not_called()

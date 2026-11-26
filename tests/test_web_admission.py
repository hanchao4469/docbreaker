import tempfile
import unittest
from pathlib import Path
from docbreaker.config import Settings
from docbreaker.web_jobs import WebJobs, CAPACITY
from docbreaker.web_fetch import WebError

class WebAdmissionTests(unittest.TestCase):
    def test_same_tenant_pages_are_admitted_and_global_queue_remains_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = WebJobs(Settings(api_key='development-test-key-only-123456789', data_dir=Path(directory)))
            jobs.root.mkdir()
            # Isolate the queue bound from the independent disk-reservation guard.
            from unittest.mock import patch
            with patch('docbreaker.web_jobs.STORAGE_LIMIT', 10**10):
                states = [jobs.submit(101, f'https://example.com/page/{n}', 1, full_site=False)
                          for n in range(CAPACITY)]
                self.assertEqual(len({s['task_id'] for s in states}), CAPACITY)
                self.assertEqual(jobs.queue.qsize(), CAPACITY)
                with self.assertRaises(WebError) as error:
                    jobs.submit(102, 'https://example.com/other', 1, full_site=False)
                self.assertEqual(error.exception.code, 'web_queue_full')

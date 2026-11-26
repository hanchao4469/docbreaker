"""Check retry accounting without sending production traffic."""
import argparse
import contextlib
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

from tools.loadtest import LoadTest, memory_bytes, percentile


class LoadTestChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "sample.pdf").write_bytes(b"%PDF-test")
        self.test = LoadTest(argparse.Namespace(output=root / "results", fixtures=root,
                            base_url="http://127.0.0.1:8000", max_wall=10, task_poll_timeout=2), "private-value")
        self.test.build = "build"
        self.spec = {"id": 1, "source": "sample.pdf", "filename": "sample.pdf", "category": "test", "pages": 1}

    def tearDown(self):
        self.temp.cleanup()

    def terminal(self, status="completed"):
        return {"status": status, "build": "build", "completed_at": time.time(), "created_at": time.time() - 1,
                "page_count": 1, "processed_pages": 1, "duration_ms": 500, "queued_ms": 500,
                "files": [{"content": {"markdown": "source text"}}], "error": {"code": "task_timeout"} if status == "failed" else None}

    def run_one(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.test.run_one(self.spec)

    def test_only_explicit_429_is_retried(self):
        with patch.object(self.test, "request", side_effect=[(429, None, {"Retry-After": "5"}),
                (202, {"task_id": "task_1", "result_url": "/v1/tasks/task_1/result"}, {}),
                (200, self.terminal(), {})]) as request, patch.object(self.test.stop, "wait", return_value=False):
            row = self.run_one()
        self.assertEqual((row["status"], row["attempts"], row["http_429"]), ("completed", 2, 1))
        self.assertEqual(request.call_count, 3)
        self.assertEqual(self.test.posts, {"429": 1, "202": 1})

    def test_ambiguous_post_is_not_replayed(self):
        with patch.object(self.test, "request", side_effect=urllib.error.URLError("unknown")) as request:
            row = self.run_one()
        self.assertEqual(row["status"], "request_exception_unknown")
        self.assertEqual(request.call_count, 1)

    def test_auth_failure_stops_new_submissions(self):
        with patch.object(self.test, "request", return_value=(401, None, {})):
            row = self.run_one()
        self.assertEqual(row["status"], "submit_rejected")
        self.assertEqual(self.test.reason, "authentication_failed")
        self.assertEqual(self.run_one()["attempts"], 0)

    def test_idempotent_retry_preserves_key_and_checksum_after_unknown_post(self):
        self.test.args.idempotent_submissions = True
        with patch.object(self.test, "request", side_effect=[urllib.error.URLError("unknown"),
                (202, {"task_id": "task_1", "result_url": "/v1/tasks/task_1/result"}, {}),
                (200, self.terminal(), {})]) as request, patch.object(self.test.stop, "wait", return_value=False):
            row = self.run_one()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["submit_network_retries"], 1)
        first, second = request.call_args_list[:2]
        self.assertEqual(first.args[3]["Idempotency-Key"], second.args[3]["Idempotency-Key"])
        self.assertEqual(len(first.args[3]["X-Content-SHA256"]), 64)

    def test_server_timeout_is_not_success_or_retried(self):
        with patch.object(self.test, "request", side_effect=[
                (202, {"task_id": "task_1", "result_url": "/v1/tasks/task_1/result"}, {}),
                (200, self.terminal("failed"), {})]):
            row = self.run_one()
        self.assertEqual((row["status"], row["error_code"], row["attempts"]), ("failed", "task_timeout", 1))
        self.assertEqual(row["queue_s"], .5)

    def test_statistics_and_docker_memory_units(self):
        self.assertEqual(percentile([1, 2, 3], 50), 2)
        self.assertEqual(memory_bytes("3 GiB"), 3 * 1024**3)
        self.assertEqual(memory_bytes("128 MiB"), 128 * 1024**2)
        self.assertEqual(memory_bytes("77.1MiB"), 77.1 * 1024**2)


if __name__ == "__main__":
    unittest.main()

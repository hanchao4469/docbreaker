"""Behavioral checks for real Poppler parsing, task lifecycle and LiteLLM transport."""

import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from PIL import Image

from docbreaker.api import create_app
from docbreaker.config import Settings
from docbreaker.events import ParseError
from docbreaker.jobs import Jobs, write_json
from docbreaker.model_client import ModelClient, SYSTEM_PROMPT
from docbreaker.pdf_tools import PdfTools, route_reasons
from docbreaker.parser import local_markdown
from deploy.smoke import sample_pdf

KEY = "test-secret-" + "0" * 32
AUTH = {"Authorization": "Bearer " + KEY, "Content-Type": "application/octet-stream"}


def bbox(lines):
    return ("<html><page width='595' height='842'>" + "".join(
        "<line>" + "".join(f"<word xMin='{x}' yMin='{y}' xMax='{x+25}' yMax='{y+12}'>word</word>"
                             for x, y in line) + "</line>" for line in lines) + "</page></html>").encode()


class RoutingTests(unittest.TestCase):
    def test_clean_text_and_small_logo_stay_local(self):
        self.assertEqual(route_reasons("Battery is rated 100Ah.\nVoltage is 51.2V.", bbox([[(50, 50), (80, 50)], [(50, 70)]]), .005), [])

    def test_corrupt_fonts_and_scan_go_to_model(self):
        self.assertIn("invalid_glyphs", route_reasons("100\ue001Ah", bbox([]), 0))
        self.assertIn("no_native_text", route_reasons("", bbox([]), 1))
        self.assertIn("image_content", route_reasons("Product photo", bbox([]), .3))

    def test_table_and_columns_go_to_model(self):
        self.assertIn("table_or_columns", route_reasons("Items and quantities", bbox([[(50, 50), (300, 50)], [(50, 70), (300, 70)]]), 0))
        self.assertIn("disjoint_text_blocks", route_reasons("Left and right", bbox([[(50, 50)], [(300, 50)]]), 0))

    def test_bad_xml_goes_to_model(self):
        self.assertIn("layout_uncertain", route_reasons("Source", b"invalid", 0))

    def test_local_language_and_numbers_preserved(self):
        self.assertEqual(local_markdown("  中文原文51.2V\n English 100Ah\n\n  2 AWG"), "中文原文51.2V\nEnglish 100Ah\n\n2 AWG")
        self.assertIn("Preserve the original language", SYSTEM_PROMPT)
        self.assertIn("Do not translate", SYSTEM_PROMPT)

    def test_literal_markdown_image_is_escaped(self):
        self.assertIn("!\\[", local_markdown("![diagram](relative.jpg)"))

    def test_configuration_rejects_bad_urls_and_empty_key(self):
        with self.assertRaises(ValueError):
            Settings(api_key="")
        for url in ["file:///tmp/test", "https://user:pass@host", "https://host/v1"]:
            with self.assertRaises(ValueError):
                Settings(api_key=KEY, litellm_base_url=url, litellm_api_key="key")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(api_key=KEY, data_dir=Path(self.temp.name))
        self.app = create_app(self.settings)
        self.context = TestClient(self.app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temp.cleanup()

    def submit(self, data=None):
        response = self.client.post("/v1/tasks?filename=test.pdf&tier=basic&output_formats=markdown",
                                    content=data if data is not None else sample_pdf(), headers=AUTH)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()["result_url"].startswith("/v1/"))
        return response.json()

    def wait(self, task):
        for _ in range(300):
            result = self.client.get(task["result_url"], headers=AUTH).json()
            if result["status"] in {"failed", "completed"}:
                return result
            time.sleep(.01)
        self.fail("Worker did not finish")

    def test_authentication_on_all_routes(self):
        for url in ["/v1/health", "/v1/tasks/task_abc/result", "/v1/tasks/task_abc/artifacts/run.json"]:
            self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.post("/v1/tasks", content=sample_pdf()).status_code, 401)
        self.assertEqual(self.client.get("/v1/health", headers=AUTH).status_code, 200)

    def test_rollout_drain_rejects_new_jobs_then_resumes(self):
        self.assertEqual(self.client.post("/v1/admin/drain").status_code, 401)
        self.assertEqual(self.client.post("/v1/admin/drain", headers=AUTH).status_code, 200)
        self.assertFalse(self.client.get("/v1/health", headers=AUTH).json()["accepting"])
        response = self.client.post("/v1/tasks", content=sample_pdf(), headers=AUTH)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "service_draining")
        self.assertEqual(self.client.post("/v1/admin/resume", headers=AUTH).status_code, 200)
        self.assertEqual(self.wait(self.submit())["status"], "completed")

    def test_real_native_pdf_sns_contract_and_zero_model_calls(self):
        result = self.wait(self.submit())
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["model_calls"], 0)
        self.assertNotIn("<!-- source-page:", result["files"][0]["content"]["markdown"])
        self.assertEqual(result["pages"][0]["parser"], "local")
        text = result["files"][0]["content"]["markdown"]
        for fact in ["DocBreaker deployment check", "51.2V", "100Ah"]:
            self.assertIn(fact, text)
        for name in ["knowledge.md", "raw.txt", "run.json"]:
            response = self.client.get(f"/v1/tasks/{result['task_id']}/artifacts/{name}", headers=AUTH)
            self.assertEqual(response.status_code, 200)

    def test_multi_page_order_preserved(self):
        result = self.wait(self.submit(sample_pdf([["First page text in English."], ["Second page text in English."]])))
        self.assertEqual(result["status"], "completed", result)
        text = result["files"][0]["content"]["markdown"]
        self.assertLess(text.index("First page"), text.index("Second page"))
        self.assertEqual(result["model_calls"], 0)

    def test_invalid_input_releases_queue_slot(self):
        response = self.client.post("/v1/tasks", content=b"not a pdf", headers=AUTH)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.app.state.jobs.active, 0)
        self.assertEqual(list(Path(self.temp.name).glob("task_*")), [])

    def test_corrupt_pdf_is_failed_and_worker_recovers(self):
        result = self.wait(self.submit(b"%PDF-1.4\nnot valid"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "invalid_pdf")
        self.assertEqual(self.wait(self.submit())["status"], "completed")

    def test_upload_limit_and_bad_filename(self):
        response = self.client.post("/v1/tasks", content=b"x", headers={**AUTH, "Content-Length": str(self.settings.max_upload_bytes + 1)})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.client.post("/v1/tasks?filename=../test.pdf", content=sample_pdf(), headers=AUTH).status_code, 400)
        self.assertEqual(self.client.post("/v1/tasks?tier=unknown", content=sample_pdf(), headers=AUTH).status_code, 400)

    def test_missing_task_and_artifact_traversal(self):
        self.assertEqual(self.client.get("/v1/tasks/task_" + "0" * 32 + "/result", headers=AUTH).status_code, 404)
        task = self.wait(self.submit())
        self.assertEqual(self.client.get(f"/v1/tasks/{task['task_id']}/artifacts/source.pdf", headers=AUTH).status_code, 404)

    def test_scan_fails_explicitly_without_model_config(self):
        path = Path(self.temp.name) / "scan.pdf"
        Image.new("RGB", (100, 100), "red").save(path, "PDF")
        result = self.wait(self.submit(path.read_bytes()))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "model_not_configured")
        self.assertEqual(result["files"], [])

    def test_blank_page_is_skipped_without_model(self):
        result = self.wait(self.submit(sample_pdf([["Native text before a blank page."], []])))
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["pages"][1]["parser"], "blank")
        self.assertEqual(result["model_calls"], 0)


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_scanned_pdf_records_failed_model_attempt_and_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(api_key=KEY, data_dir=root, litellm_base_url="https://gateway.example",
                                litellm_api_key="test-model-key")
            jobs = Jobs(settings)
            await jobs.start()
            await jobs.client.aclose()
            jobs.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401)))
            try:
                task_id, directory = jobs.reserve("scanned.pdf", tenant_id=12345)
                Image.new("RGB", (100, 100), "red").save(directory / "source.pdf", "PDF")
                jobs.submit(task_id, (directory / "source.pdf").stat().st_size)
                await asyncio.wait_for(jobs.queue.join(), timeout=10)
                state = jobs.read(task_id)
                self.assertEqual(state["error"]["code"], "model_auth_failed")
                self.assertEqual(state["model_calls"], 1)
                self.assertEqual(state["current_page"], 1)
                self.assertEqual(state["stage"], "llm")
                self.assertEqual(state["page_count"], 1)
            finally:
                await jobs.stop()

    async def test_scanned_pdf_successful_model_path_retains_language_and_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(api_key=KEY, data_dir=root, litellm_base_url="https://gateway.example",
                                litellm_api_key="test-model-key")
            jobs = Jobs(settings)
            await jobs.start()
            await jobs.client.aclose()
            payload = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "markdown": "# Original English\n\nVoltage: 51.2V.\nCapacity: 100Ah.", "uncertain": False})}}],
                "usage": {"total_tokens": 30}}
            jobs.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
            try:
                task_id, directory = jobs.reserve("scanned.pdf", tenant_id=12345)
                Image.new("RGB", (100, 100), "red").save(directory / "source.pdf", "PDF")
                jobs.submit(task_id, (directory / "source.pdf").stat().st_size)
                await asyncio.wait_for(jobs.queue.join(), timeout=10)
                state = jobs.read(task_id)
                self.assertEqual(state["status"], "completed")
                self.assertEqual(state["model_calls"], 1)
                self.assertEqual(state["usage"]["total_tokens"], 30)
                self.assertEqual(state["pages"][0]["parser"], "llm")
                self.assertIn("Original English", (directory / "knowledge.md").read_text())
            finally:
                await jobs.stop()

    async def test_subprocess_timeout_kills_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = PdfTools(Settings(api_key=KEY, command_timeout=.02), "test")
            with self.assertRaises(ParseError) as caught:
                await tools.run("python3", ["-c", "import time;time.sleep(30)"], Path(tmp))
            self.assertEqual(caught.exception.code, "tool_timeout")

    async def test_restart_marks_pending_tasks_failed_and_retains_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i, status in enumerate(["queued", "running", "completed"]):
                directory = root / ("task_" + str(i) * 32)
                directory.mkdir()
                write_json(directory / "run.json", {"status": status})
            jobs = Jobs(Settings(api_key=KEY, data_dir=root))
            await jobs.start()
            try:
                for i in [0, 1]:
                    self.assertEqual(jobs.read("task_" + str(i) * 32)["error"]["code"], "worker_restarted")
                self.assertEqual(jobs.read("task_" + "2" * 32)["status"], "completed")
            finally:
                await jobs.stop()

    async def test_queue_capacity_is_reserved_before_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Jobs(Settings(api_key=KEY, data_dir=Path(tmp), queue_size=1, concurrency=1))
            await jobs.start()
            try:
                first, _ = jobs.reserve("first.pdf")
                second, _ = jobs.reserve("second.pdf")
                with self.assertRaises(ParseError) as caught:
                    jobs.reserve("third.pdf")
                self.assertEqual(caught.exception.code, "queue_full")
                jobs.abandon(first)
                jobs.abandon(second)
                self.assertEqual(jobs.active, 0)
            finally:
                await jobs.stop()

    async def test_second_worker_cannot_use_the_same_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(api_key=KEY, data_dir=Path(tmp))
            first, second = Jobs(settings), Jobs(settings)
            await first.start()
            try:
                with self.assertRaises(RuntimeError):
                    await second.start()
            finally:
                await first.stop()

    async def test_retention_does_not_delete_active_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i, status in enumerate(["completed", "running"]):
                directory = root / ("task_" + str(i) * 32)
                directory.mkdir()
                write_json(directory / "run.json", {"status": status, "completed_at": 0})
            jobs = Jobs(Settings(api_key=KEY, data_dir=root))
            jobs.cleanup()
            self.assertFalse((root / ("task_" + "0" * 32)).exists())
            self.assertTrue((root / ("task_" + "1" * 32)).exists())

    async def test_task_total_deadline_fails_cleanly(self):
        async def slow_parser(*args):
            await asyncio.sleep(30)
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Jobs(Settings(api_key=KEY, data_dir=Path(tmp), task_timeout=.03))
            await jobs.start()
            try:
                task_id, directory = jobs.reserve("deadline.pdf")
                (directory / "source.pdf").write_bytes(sample_pdf())
                with patch("docbreaker.jobs.parse_pdf", slow_parser):
                    jobs.submit(task_id, 100)
                    await asyncio.wait_for(jobs.queue.join(), timeout=2)
                self.assertEqual(jobs.read(task_id)["error"]["code"], "task_timeout")
                self.assertEqual(jobs.active, 0)
                self.assertTrue((directory / "source.pdf").exists())
            finally:
                await jobs.stop()


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, handler, retries=0):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "page.jpg"
            Image.new("RGB", (10, 10), "white").save(image)
            settings = Settings(api_key=KEY, litellm_base_url="https://gateway.example", litellm_api_key="secret",
                                model_retries=retries)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await ModelClient(settings, client, tenant_id=12345).read_page("task_test", 2, image, "Raw 51.2V", "battery.pdf")

    def response(self, markdown="Battery: 51.2V, 100Ah.", finish="stop", uncertain=False):
        return {"choices": [{"finish_reason": finish, "message": {"content": json.dumps({"markdown": markdown, "uncertain": uncertain})}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}

    async def test_real_request_shape_and_service_tags(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://gateway.example/v1/chat/completions")
            self.assertEqual(request.headers["x-litellm-tags"], "pdf-parse")
            self.assertEqual(request.headers["x-litellm-session-id"], "task_test")
            self.assertEqual(request.headers["authorization"], "Bearer secret")
            data = json.loads(request.content)
            self.assertEqual(data["model"], "gpt-6-luna")
            self.assertTrue(data["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
            return httpx.Response(200, json=self.response())
        result = await self.call(handler)
        self.assertEqual(result["usage"]["total_tokens"], 30)

    async def test_auth_failure_is_not_retried_and_does_not_expose_response(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(401, text="private-key-secret")
        with self.assertRaises(ParseError) as caught:
            await self.call(handler, retries=1)
        self.assertEqual(caught.exception.code, "model_auth_failed")
        self.assertNotIn("private-key-secret", str(caught.exception))
        self.assertEqual(len(calls), 1)

    async def test_transient_server_error_retried(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=self.response())
        result = await self.call(handler, retries=1)
        self.assertIn("51.2V", result["markdown"])
        self.assertEqual(len(calls), 2)

    async def test_truncation_never_returns_partial_knowledge(self):
        with self.assertRaises(ParseError) as caught:
            await self.call(lambda request: httpx.Response(200, json=self.response(finish="length")))
        self.assertEqual(caught.exception.code, "model_incomplete")

    async def test_invalid_envelope_and_empty_output_rejected(self):
        for payload in [{"choices": []}, self.response(markdown="")]:
            with self.assertRaises(ParseError) as caught:
                await self.call(lambda request: httpx.Response(200, json=payload))
            self.assertIn(caught.exception.code, {"invalid_model_output", "empty_model_output"})


if __name__ == "__main__":
    unittest.main()

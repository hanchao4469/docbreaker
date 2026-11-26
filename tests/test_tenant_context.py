"""Tenant attribution survives async jobs, retries, restarts and concurrent requests."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from docbreaker.api import create_app
from docbreaker.config import Settings
from docbreaker.events import ParseError
from docbreaker.jobs import Jobs
from docbreaker.model_client import ModelClient
from deploy.smoke import sample_pdf
from tests.test_service import KEY, AUTH
from tests import test_loadtest


def reply():
    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
        'content': json.dumps({'markdown': 'Original page facts 100Ah', 'uncertain': False})}}]})


class TenantModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_tenant_prevents_any_unattributed_model_request(self):
        calls = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: calls.append(req))) as client:
            model = ModelClient(Settings(api_key=KEY, litellm_base_url='https://gateway.example',
                                        litellm_api_key='test'), client)
            with self.assertRaises(ParseError) as caught:
                await model.read_page('task_test', 1, Path('unused.jpg'), '', 'test.pdf')
        self.assertEqual(caught.exception.code, 'tenant_id_required')
        self.assertEqual(calls, [])
        self.assertEqual(model.calls, 0)

    async def test_concurrent_tenants_and_format_retries_keep_their_own_header(self):
        calls = {12345: [], 67890: []}
        async def handler(request):
            tenant = int(request.headers['x-litellm-end-user-id'])
            calls[tenant].append(request)
            await asyncio.sleep(.01)
            return reply() if len(calls[tenant]) > 1 else httpx.Response(200, json={
                'choices': [{'finish_reason': 'stop', 'message': {'content': '{"markdown":'}}]})
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp)/'page.jpg'
            Image.new('RGB', (10,10), 'red').save(image)
            settings = Settings(api_key=KEY, litellm_base_url='https://gateway.example', litellm_api_key='test')
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                models = [ModelClient(settings, client, tenant_id=tenant) for tenant in calls]
                results = await asyncio.gather(*(m.read_page(f'task_{m.tenant_id}', 1, image, '', 'test.pdf') for m in models))
            self.assertTrue(all(r['markdown'] for r in results))
            for tenant, requests in calls.items():
                self.assertEqual(len(requests), 2)
                self.assertTrue(all(r.headers['x-litellm-end-user-id'] == str(tenant) for r in requests))
                self.assertTrue(all(r.headers['x-litellm-tags'] == 'pdf-parse' for r in requests))
                self.assertTrue(all(r.headers['x-litellm-session-id'] == f'task_{tenant}' for r in requests))

    async def test_failed_job_retry_after_restart_keeps_persisted_tenant(self):
        headers = []
        def handler(request):
            headers.append(request.headers['x-litellm-end-user-id'])
            return httpx.Response(503) if len(headers) == 1 else reply()
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(api_key=KEY, data_dir=Path(tmp), litellm_base_url='https://gateway.example',
                                litellm_api_key='test', model_retries=0)
            first = Jobs(settings)
            await first.start()
            await first.client.aclose()
            first.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                task_id, directory = first.reserve('scan.pdf', tenant_id=12345)
                Image.new('RGB', (10,10), 'red').save(directory/'source.pdf', 'PDF')
                first.submit(task_id, (directory/'source.pdf').stat().st_size)
                await asyncio.wait_for(first.queue.join(), 10)
                self.assertEqual(first.read(task_id)['status'], 'failed')
            finally:
                await first.stop()
            second = Jobs(settings)
            await second.start()
            await second.client.aclose()
            second.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                second.retry(task_id)
                await asyncio.wait_for(second.queue.join(), 10)
                state = second.read(task_id)
                self.assertEqual(state['status'], 'completed', state.get('error'))
                self.assertEqual(state['tenant_id'], 12345)
                self.assertEqual(headers, ['12345', '12345'])
            finally:
                await second.stop()


class TenantApiTests(unittest.TestCase):
    def test_diagram_download_requires_authentication_and_matching_tenant(self):
        import json
        with tempfile.TemporaryDirectory() as tmp, TestClient(create_app(Settings(api_key=KEY, data_dir=Path(tmp)))) as client:
            jobs = client.app.state.jobs
            task_id, directory = jobs.reserve("diagram.pdf", tenant_id=101)
            state = jobs.read(task_id)
            state.update(status="completed", pages=[{"figures": [{"name": "figure-1-1.jpg", "page": 1}]}])
            (directory / "run.json").write_text(json.dumps(state))
            (directory / "knowledge.md").write_text("Insert latch from the left.")
            (directory / "figure-1-1.jpg").write_bytes(b"source crop")
            (directory / "figure-1-2.jpg").write_bytes(b"unlisted crop")
            base = f"/v1/tasks/{task_id}"
            result = client.get(base + "/result", headers=AUTH).json()
            self.assertEqual(result["files"][0]["content"]["figures"], [{"name": "figure-1-1.jpg", "page": 1}])
            url = base + "/figures/figure-1-1.jpg"
            self.assertEqual(client.get(url + "?tenant_id=101").status_code, 401)
            self.assertEqual(client.get(url + "?tenant_id=102", headers=AUTH).status_code, 404)
            self.assertEqual(client.get(base + "/figures/figure-1-2.jpg?tenant_id=101", headers=AUTH).status_code, 404)
            response = client.get(url + "?tenant_id=101", headers=AUTH)
            self.assertEqual(response.headers["content-type"], "image/jpeg")
            self.assertEqual(response.content, b"source crop")

    def test_invalid_tenant_rejected_before_reserving_upload(self):
        with tempfile.TemporaryDirectory() as tmp, TestClient(create_app(Settings(api_key=KEY, data_dir=Path(tmp)))) as client:
            for tenant in ('0', '-1', 'bad', str(2**63)):
                result = client.post(f'/v1/tasks?tenant_id={tenant}', content=sample_pdf(), headers=AUTH)
                self.assertEqual(result.status_code, 422)
            self.assertFalse(list(Path(tmp).glob('task_*')))

    def test_tenant_scopes_idempotency_and_survives_api_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(api_key=KEY, data_dir=Path(tmp))
            headers = {**AUTH, 'Idempotency-Key': 'same-business-id'}
            with TestClient(create_app(settings)) as client:
                first = client.post('/v1/tasks?tenant_id=12345', content=sample_pdf(), headers=headers).json()
                other = client.post('/v1/tasks?tenant_id=67890', content=sample_pdf(), headers=headers).json()
                self.assertNotEqual(first['task_id'], other['task_id'])
                for task in (first, other):
                    for _ in range(100):
                        state = client.get(task['result_url'], headers=AUTH).json()
                        if state['status'] == 'completed': break
                        time.sleep(.01)
                    self.assertEqual(state['status'], 'completed')
            with TestClient(create_app(settings)) as client:
                replay = client.post('/v1/tasks?tenant_id=12345', content=sample_pdf(), headers=headers).json()
                self.assertEqual(replay['task_id'], first['task_id'])
                self.assertTrue(replay['replayed'])
                state = client.get(first['result_url'], headers=AUTH).json()
                self.assertEqual(state['tenant_id'], 12345)


class TenantScriptTests(unittest.TestCase):
    def test_loadtest_transmits_explicit_script_tenant(self):
        # Reuse the fixture harness without performing HTTP or starting a benchmark.
        harness = test_loadtest.LoadTestChecks()
        harness.setUp()
        try:
            harness.test.args.tenant_id = 98765
            with patch.object(harness.test, 'request', side_effect=[
                    (202, {'task_id':'task_1', 'result_url':'/v1/tasks/task_1/result'}, {}),
                    (200, harness.terminal(), {})]) as request:
                row = harness.run_one()
            self.assertEqual(parse_qs(urlsplit(request.call_args_list[0].args[0]).query)['tenant_id'], ['98765'])
            self.assertEqual(row['tenant_id'], 98765)
        finally:
            harness.tearDown()

"""Regression tests for bounded concurrency, durable work and local-first routing."""
import asyncio
import hashlib
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
from docbreaker.layout import prose_columns
from docbreaker.pdf_tools import PdfTools
from deploy.smoke import sample_pdf
from tests.test_service import KEY, AUTH


def completed():
    return {'page_count': 1, 'processed_pages': 1, 'pages': [], 'usage': {}, 'model_calls': 0}


def enqueue(jobs, name='test.pdf'):
    task_id, directory = jobs.reserve(name, tenant_id=12345)
    (directory/'source.pdf').write_bytes(sample_pdf())
    jobs.submit(task_id, (directory/'source.pdf').stat().st_size)
    return task_id


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_ten_workers_admit_ten_documents_and_never_start_eleventh(self):
        entered, release = [], asyncio.Event()
        async def parser(source, filename, task_id, *args):
            entered.append(task_id)
            await release.wait()
            return completed()
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Jobs(Settings(api_key=KEY, data_dir=Path(tmp), concurrency=10, queue_size=2))
            await jobs.start()
            try:
                with patch('docbreaker.jobs.parse_pdf', parser):
                    ids = [enqueue(jobs) for _ in range(12)]
                    with self.assertRaises(ParseError):
                        jobs.reserve('overflow.pdf')
                    for _ in range(100):
                        if len(entered) == 10:
                            break
                        await asyncio.sleep(.005)
                    self.assertEqual(len(entered), 10)
                    self.assertEqual(len(jobs.current), 10)
                    self.assertEqual(jobs.queue.qsize(), 2)
                    release.set()
                    await asyncio.wait_for(jobs.queue.join(), 2)
                    self.assertTrue(all(jobs.read(task_id)['status'] == 'completed' for task_id in ids))
            finally:
                await jobs.stop()

    async def test_queue_wait_does_not_consume_processing_budget(self):
        async def parser(*args):
            await asyncio.sleep(.08)
            return completed()
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Jobs(Settings(api_key=KEY, data_dir=Path(tmp), concurrency=1, task_timeout=.13))
            await jobs.start()
            try:
                with patch('docbreaker.jobs.parse_pdf', parser):
                    ids = [enqueue(jobs) for _ in range(3)]
                    await asyncio.wait_for(jobs.queue.join(), 2)
                self.assertTrue(all(jobs.read(task_id)['status'] == 'completed' for task_id in ids))
                self.assertGreater(jobs.read(ids[-1])['queued_ms'], 130)
            finally:
                await jobs.stop()

    async def test_queue_deadline_is_distinct_from_processing_timeout(self):
        async def parser(*args):
            await asyncio.sleep(.08)
            return completed()
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Jobs(Settings(api_key=KEY, data_dir=Path(tmp), concurrency=1, queue_timeout=.03))
            await jobs.start()
            try:
                with patch('docbreaker.jobs.parse_pdf', parser):
                    first, second = enqueue(jobs), enqueue(jobs)
                    await asyncio.wait_for(jobs.queue.join(), 2)
                self.assertEqual(jobs.read(first)['status'], 'completed')
                self.assertEqual(jobs.read(second)['error']['code'], 'queue_timeout')
            finally:
                await jobs.stop()

    async def test_restart_recovers_an_accepted_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_id = 'task_' + 'a'*32
            directory = root/task_id
            directory.mkdir()
            (directory/'source.pdf').write_bytes(sample_pdf())
            write_json(directory/'run.json', {'task_id': task_id, 'filename': 'recovered.pdf',
                       'status': 'running', 'created_at': time.time(), 'pages': [], 'usage': {}})
            jobs = Jobs(Settings(api_key=KEY, data_dir=root))
            with patch('docbreaker.jobs.parse_pdf', return_value=completed()):
                await jobs.start()
                try:
                    await asyncio.wait_for(jobs.queue.join(), 2)
                    state = jobs.read(task_id)
                    self.assertEqual(state['status'], 'completed')
                    self.assertTrue(state['recovered'])
                finally:
                    await jobs.stop()

    async def test_failed_page_falls_back_without_rebilling_successful_page(self):
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 2:
                return httpx.Response(503)
            text = 'Original page one.' if len(calls) == 1 else 'Original page two.'
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps({'markdown': text, 'uncertain': False})}}]})
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Jobs(Settings(api_key=KEY, data_dir=Path(tmp), litellm_base_url='https://gateway.example',
                                 litellm_api_key='test', model_retries=0, page_concurrency=1))
            await jobs.start()
            await jobs.client.aclose()
            jobs.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                task_id, directory = jobs.reserve('scan.pdf', tenant_id=12345)
                Image.new('RGB', (100,100), 'red').save(directory/'source.pdf', 'PDF', save_all=True,
                                                       append_images=[Image.new('RGB',(100,100),'blue')])
                jobs.submit(task_id, (directory/'source.pdf').stat().st_size)
                await asyncio.wait_for(jobs.queue.join(), 5)
                state = jobs.read(task_id)
                self.assertEqual(state['status'], 'completed')
                self.assertEqual(state['missing_content_pages'], [2])
                self.assertEqual(state['degraded_pages'][0]['code'], 'model_unavailable')
                self.assertEqual(len(calls), 2)
                text = (directory/'knowledge.md').read_text()
                self.assertIn('Original page one.', text)
            finally:
                await jobs.stop()

    async def test_invalid_bbox_uses_pdfinfo_and_visual_route(self):
        async def run(tool, args, *unused):
            if tool == 'pdfinfo':
                return b'Page    8 size: 612 x 792 pts'
            if tool == 'pdfimages':
                return b''
            return b'broken XML' if '-bbox-layout' in args else b'Original text'
        with tempfile.TemporaryDirectory() as tmp:
            tools = PdfTools(Settings(api_key=KEY), 'test')
            with patch.object(tools, 'run', run):
                page = await tools.extract(Path(tmp)/'source.pdf', 8)
            self.assertEqual((page.width, page.height), (612, 792))
            self.assertIn('layout_uncertain', page.reasons)
            self.assertEqual(page.text, 'Original text')


class UploadTests(unittest.TestCase):
    def test_same_idempotency_key_replays_but_different_content_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Settings(api_key=KEY, data_dir=Path(tmp)))
            headers = {**AUTH, 'Idempotency-Key': 'unique-test-request'}
            data = sample_pdf()
            with TestClient(app) as client:
                first = client.post('/v1/tasks?filename=test.pdf', content=data, headers=headers)
                again = client.post('/v1/tasks?filename=test.pdf', content=data, headers=headers)
                self.assertEqual((first.status_code, again.status_code), (202,202))
                self.assertEqual(first.json()['task_id'], again.json()['task_id'])
                self.assertTrue(again.json()['replayed'])
                self.assertEqual(len(list(Path(tmp).glob('task_*'))), 1)
                mismatch = client.post('/v1/tasks?filename=test.pdf', content=sample_pdf([['Different text']]), headers=headers)
                self.assertEqual(mismatch.status_code, 409)

    def test_large_rejected_upload_is_consumed_so_client_can_read_429(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Settings(api_key=KEY, data_dir=Path(tmp), concurrency=1, queue_size=1))
            chunks = []
            def body():
                for n in range(8):
                    chunks.append(n)
                    yield b'x' * 512 * 1024
            with TestClient(app) as client:
                ids = [app.state.jobs.reserve('held.pdf')[0] for _ in range(2)]
                response = client.post('/v1/tasks', content=body(), headers=AUTH)
                self.assertEqual(response.status_code, 429)
                self.assertEqual(len(chunks), 8)
                self.assertEqual(response.headers['Retry-After'], '5')
                for task_id in ids:
                    app.state.jobs.abandon(task_id)

    def test_checksum_mismatch_does_not_leave_reserved_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Settings(api_key=KEY, data_dir=Path(tmp)))
            with TestClient(app) as client:
                result = client.post('/v1/tasks', content=sample_pdf(), headers={**AUTH, 'X-Content-SHA256': '0'*64})
                self.assertEqual(result.status_code, 400)
                self.assertEqual(app.state.jobs.active, 0)
                self.assertFalse(list(Path(tmp).glob('task_*')))

    def test_completed_idempotency_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(api_key=KEY, data_dir=Path(tmp))
            headers = {**AUTH, 'Idempotency-Key': 'persistent-key'}
            with TestClient(create_app(settings)) as client:
                submitted = client.post('/v1/tasks', content=sample_pdf(), headers=headers).json()
                for _ in range(100):
                    state = client.get(submitted['result_url'], headers=AUTH).json()
                    if state['status'] == 'completed':
                        break
                    time.sleep(.01)
                self.assertEqual(state['status'], 'completed')
            with TestClient(create_app(settings)) as client:
                replay = client.post('/v1/tasks', content=sample_pdf(), headers=headers)
                self.assertEqual(replay.status_code, 202)
                self.assertEqual(replay.json()['task_id'], submitted['task_id'])


class LayoutTests(unittest.TestCase):
    def test_prose_columns_restore_reading_order_without_dropping_glyphs(self):
        lines, native = [], []
        for i in range(8):
            for column, x in [('left',30),('right',330)]:
                words = [column+str(i), 'original', 'text', 'with', 'facts', '100Ah']
                native.extend(words)
                lines.append('<line>' + ''.join(f'<word xMin="{x+j*35}" yMin="{50+i*20}" xMax="{x+j*35+30}" yMax="{62+i*20}">{word}</word>' for j,word in enumerate(words)) + '</line>')
        xml = ('<html><page width="600" height="800">'+''.join(lines)+'</page></html>').encode()
        text = prose_columns(xml, ' '.join(native))
        self.assertIsNotNone(text)
        self.assertLess(text.index('left7'), text.index('right0'))
        self.assertEqual(sorted(text.split()), sorted(native))
        self.assertIsNone(prose_columns(xml, ' '.join(native)+' missing'))

    def test_short_numeric_cells_are_not_treated_as_prose(self):
        from tests.test_service import bbox
        self.assertIsNone(prose_columns(bbox([[(50,50),(300,50)],[(50,70),(300,70)]]), 'word '*4))

    def test_configuration_cannot_exceed_requested_maximum(self):
        for name, invalid in (('concurrency',21),('page_concurrency',11),('upload_concurrency',11)):
            with self.assertRaises(ValueError):
                Settings(api_key=KEY, **{name:invalid})
        self.assertEqual(Settings(api_key=KEY, concurrency=20).concurrency, 20)
        self.assertEqual(Settings(api_key=KEY, model_concurrency=100).model_concurrency, 100)
        with self.assertRaises(ValueError):
            Settings(api_key=KEY, model_concurrency=101)
        with self.assertRaises(ValueError):
            Settings(api_key=KEY, task_timeout=float('nan'))


if __name__ == '__main__':
    unittest.main()

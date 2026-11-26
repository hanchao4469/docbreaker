"""Verify local-first routing and the global HTTP cap, without paid model calls."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from docbreaker.concurrency import ConcurrencyLimiter
from docbreaker.config import Settings
from docbreaker.jobs import Jobs
from docbreaker.layout import local_layout, text_and_rules_only
from docbreaker.pdf_tools import Page, PdfTools
from tests.test_optimizations import enqueue
from tests.test_service import KEY


def grid_xml(rows):
    """Independent positioned cells matching native content, like Poppler bbox output."""
    lines = []
    for y, row in enumerate(rows):
        for x, value in enumerate(row):
            lines.append(f'<line><word xMin="{40+x*180}" yMin="{100+y*25}" '
                         f'xMax="{40+x*180+len(value)*5}" yMax="{112+y*25}">{value}</word></line>')
    return ('<html><page width="600" height="800">' + ''.join(lines) + '</page></html>').encode()


def table_pdf(with_figure=False):
    """A real PDF with independently positioned columns and optional vector drawing."""
    commands = []
    for i, row in enumerate((('Item', 'Quantity'), ('Battery', '1'), ('Bolt', '10'), ('Cable', '4'))):
        for j, value in enumerate(row):
            commands.append(f'BT /F1 12 Tf 1 0 0 1 {50+j*200} {700-i*25} Tm ({value}) Tj ET')
    if with_figure:
        commands.append('50 200 100 100 re S')
    content = '\n'.join(commands).encode()
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [4 0 R] /Count 1 >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>',
               b'<< /Length '+str(len(content)).encode()+b' >>\nstream\n'+content+b'\nendstream']
    output=b'%PDF-1.4\n'
    offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(output));output+=f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n'
    xref=len(output)
    output+=b'xref\n0 6\n0000000000 65535 f \n'+b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    return output+f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()


class LocalLayoutTests(unittest.TestCase):
    def test_simple_native_table_keeps_item_quantity_associations(self):
        rows = [('Item', 'Quantity'), ('Battery', '1'), ('Bolt', '10'), ('Cable', '4')]
        native = '\n'.join(' '.join(row) for row in rows)
        text, kind = local_layout(grid_xml(rows), native)
        self.assertEqual(kind, 'simple_table')
        for item, quantity in rows[1:]:
            self.assertIn(f'| {item} | {quantity} |', text)
        self.assertIsNone(local_layout(grid_xml(rows), native + ' dropped'))

    def test_merged_header_or_missing_cell_is_not_guessed(self):
        rows = [('Item', 'Quantity'), ('Battery', '1'), ('Bolt',), ('Cable', '4')]
        self.assertIsNone(local_layout(grid_xml(rows), ' '.join(value for row in rows for value in row)))

    def test_vector_chart_cannot_be_mistaken_for_native_text(self):
        self.assertTrue(text_and_rules_only(b'<svg><defs><path d="M0 0L1 1Z"/></defs><g><use href="#glyph0-1"/></g></svg>'))
        self.assertTrue(text_and_rules_only(b'<svg><path fill="none" stroke-width="1" d="M 0 0 L 100 0"/></svg>'))
        for shape in (b'<path fill="none" d="M 0 0 L 20 30 L 40 10"/>',
                      b'<rect width="200" height="100"/>', b'<image href="data:image/png;base64,x"/>',
                      b'<use href="#diagram"/>'):
            self.assertFalse(text_and_rules_only(b'<svg>' + shape + b'</svg>'))


class ParallelPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_table_uses_zero_models_but_vector_figure_keeps_visual_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Jobs(Settings(api_key=KEY, data_dir=Path(tmp)))
            await jobs.start()
            try:
                task_id, directory = jobs.reserve('table.pdf')
                (directory/'source.pdf').write_bytes(table_pdf())
                jobs.submit(task_id,(directory/'source.pdf').stat().st_size)
                await asyncio.wait_for(jobs.queue.join(),10)
                state=jobs.read(task_id)
                self.assertEqual(state['status'],'completed',state.get('error'))
                self.assertEqual(state['model_calls'],0)
                text=(directory/'knowledge.md').read_text()
                self.assertIn('| Bolt | 10 |',text)
                self.assertIn('| Cable | 4 |',text)
                source=Path(tmp)/'with-figure.pdf'
                source.write_bytes(table_pdf(True))
                page=await PdfTools(jobs.settings,'figure-check').extract(source,1)
                self.assertTrue(page.reasons,'The vector figure must not be silently discarded')
            finally:
                await jobs.stop()

    async def test_sixty_pages_across_six_documents_share_fifty_model_slots(self):
        active = peak = entered = 0
        release = asyncio.Event()

        async def handler(request):
            nonlocal active, peak, entered
            context = json.loads(request.content)['messages'][1]['content'][0]['text']
            page = json.loads(context.split('Source data follows:\n', 1)[1])['page']
            active += 1
            entered += 1
            peak = max(peak, active)
            try:
                await release.wait()
                await asyncio.sleep((10-page)*.001)  # Completion order differs from source order.
                return httpx.Response(200, json={'model': 'actual-test-model', 'choices': [
                    {'finish_reason': 'stop', 'message': {'content': json.dumps(
                        {'markdown': f'Original page {page}.', 'uncertain': False})}}],
                    'usage': {'total_tokens': 3}})
            finally:
                active -= 1

        async def inspect(*unused):
            return 10

        async def extract(self, source, number):
            return Page(number, '', ['no_native_text'], 595, 842)

        async def render(self, source, number, scale=1):
            path = source.parent / f'page-{number}.jpg'
            Image.new('RGB', (4, 4), 'red').save(path)
            return path

        with tempfile.TemporaryDirectory() as tmp:
            jobs = Jobs(Settings(api_key=KEY, data_dir=Path(tmp), model_concurrency=50,
                                 litellm_base_url='https://gateway.example', litellm_api_key='test'))
            await jobs.start()
            await jobs.client.aclose()
            jobs.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                with patch.object(PdfTools, 'inspect', inspect), patch.object(PdfTools, 'extract', extract), patch.object(PdfTools, 'render', render):
                    ids = [enqueue(jobs) for _ in range(6)]
                    for _ in range(300):
                        if entered == 50:
                            break
                        await asyncio.sleep(.01)
                    self.assertEqual(entered, 50)
                    self.assertEqual(jobs.model_limiter.active, 50)
                    self.assertEqual(jobs.model_limiter.peak, 50)
                    self.assertEqual(jobs.page_limiter.active, 50)
                    release.set()
                    await asyncio.wait_for(jobs.queue.join(), 10)
                self.assertEqual(entered, 60)
                self.assertEqual(peak, 50)
                self.assertEqual(active, 0)
                self.assertEqual(jobs.model_limiter.active, 0)
                for task_id in ids:
                    state = jobs.read(task_id)
                    self.assertEqual(state['status'], 'completed', state.get('error'))
                    self.assertEqual(state['model_calls'], 10)
                    self.assertEqual(state['usage']['total_tokens'], 30)
                    self.assertEqual([p['page'] for p in state['pages']], list(range(1, 11)))
                    text = (jobs.folder(task_id)/'knowledge.md').read_text()
                    self.assertEqual([text.index(f'Original page {i}.') for i in range(1,11)],
                                     sorted(text.index(f'Original page {i}.') for i in range(1,11)))
            finally:
                release.set()
                await jobs.stop()

    async def test_cancelled_waiter_does_not_steal_or_leak_a_slot(self):
        limiter = ConcurrencyLimiter(1)
        async with limiter:
            async def wait():
                async with limiter:
                    self.fail('Cancelled waiter acquired the slot')
            task = asyncio.create_task(wait())
            await asyncio.sleep(0)
            self.assertEqual(limiter.waiting, 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(limiter.waiting, 0)
        async with asyncio.timeout(1):
            async with limiter:
                self.assertEqual(limiter.active, 1)
        self.assertEqual(limiter.active, 0)

    async def test_concurrent_tool_output_is_not_cross_contaminated(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = PdfTools(Settings(api_key=KEY), 'isolation-test')
            outputs = await asyncio.gather(*(tools.run('python3', ['-c', f'import time;time.sleep(.03);print({i})'], Path(tmp), i)
                                            for i in range(12)))
            self.assertEqual(outputs, [f'{i}\n'.encode() for i in range(12)])
            self.assertFalse(list(Path(tmp).glob('.*.out')))


if __name__ == '__main__':
    unittest.main()

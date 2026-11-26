"""Failed/cancelled calls must not pin image payloads through tracebacks."""
import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from PIL import Image

from docbreaker.config import Settings
from docbreaker.events import ParseError
from docbreaker.model_client import ModelClient
from docbreaker.web_enrich import WebEnricher, image_data


def frames(error):
    trace = error.__traceback__
    while trace:
        yield trace.tb_frame
        trace = trace.tb_next


class MemoryReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_pdf_failure_and_cancel_release_image_payload_from_traceback(self):
        for cancel in (False, True):
            entered = asyncio.Event()
            async def handler(request):
                entered.set()
                if cancel:
                    await asyncio.Event().wait()
                return httpx.Response(401)
            settings = Settings(api_key='a'*32, litellm_base_url='https://example.com',
                                litellm_api_key='test', model_retries=0)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'page.jpg'
                with Image.new('RGB', (200, 200), 'red') as image:
                    image.save(path)
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    model = ModelClient(settings, client, tenant_id=101)
                    task = asyncio.create_task(model.read_page('test', 1, path, '', 'test.pdf'))
                    await entered.wait()
                    if cancel:
                        task.cancel()
                    try:
                        await task
                        self.fail('Expected failure')
                    except (ParseError, asyncio.CancelledError) as error:
                        frame = next(f for f in frames(error) if f.f_code.co_name == '_read_page')
                        self.assertEqual(frame.f_locals['body'], {})
                        for name in ('image_url', 'request_body', 'response', 'content'):
                            self.assertIsNone(frame.f_locals[name], name)
                        self.assertNotIn('image_bytes', frame.f_locals)
                    self.assertEqual(model.limiter.active, 0)

    async def test_web_failed_call_does_not_pin_image_list(self):
        with io.BytesIO() as out:
            with Image.new('RGB', (200, 200), 'red') as image:
                image.save(out, 'PNG')
            data = out.getvalue()
        # Conversion remains a valid image after explicit temporary-image closure.
        self.assertTrue(image_data(data).startswith('data:image/jpeg;base64,'))
        fetcher = AsyncMock()
        fetcher.get.return_value = ('https://example.com/map.png', data, 'image/png')
        settings = Settings(api_key='a'*32, litellm_base_url='https://example.com', litellm_api_key='pdf-test', litellm_web_api_key='test')
        enricher = WebEnricher(settings, fetcher, 'web-parse')
        await enricher.client.aclose()
        enricher.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(401)))
        try:
            try:
                await enricher.analyze('test', 101, 'https://example.com/',
                                       ['https://example.com/map.png'], AsyncMock())
                self.fail('Expected failure')
            except Exception as error:
                frame = next(f for f in frames(error) if f.f_code.co_name == 'analyze')
                self.assertEqual(frame.f_locals['parts'], [])
                self.assertEqual(frame.f_locals['request'], {})
                for name in ('data', 'encoded', 'response', 'raw'):
                    self.assertIsNone(frame.f_locals[name], name)
        finally:
            await enricher.close()

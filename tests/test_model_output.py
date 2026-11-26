"""Regression coverage for observed format failures, billing, and resumable pages."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from docbreaker.config import Settings
from docbreaker.events import ParseError
from docbreaker.jobs import Jobs
from docbreaker.model_client import ModelClient
from docbreaker.model_output import parse_page_output
from docbreaker.pdf_tools import PdfTools
from tests.test_service import KEY


def response(content=None, finish='stop', **message_fields):
    if content is None:
        content = json.dumps({'markdown': 'Voltage: 51.2V. 数量：10。', 'uncertain': False,
                              'uncertainty_reasons': []}, ensure_ascii=False)
    return httpx.Response(200, json={'model': 'gateway-model', 'choices': [
        {'finish_reason': finish, 'message': {'content': content, **message_fields}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}})


class OutputNormalizationTests(unittest.TestCase):
    def test_fenced_or_prefixed_json_and_content_blocks_retain_exact_facts(self):
        data = json.dumps({'markdown': '51.2V / SC50-8 / 数量：10', 'uncertain': False}, ensure_ascii=False)
        for raw in ['```json\n' + data + '\n```', 'Result:\n' + data,
                    [{'type': 'text', 'text': data}]]:
            result = parse_page_output(raw)
            self.assertEqual(result['markdown'], '51.2V / SC50-8 / 数量：10')
            self.assertFalse(result['uncertain'])
            self.assertTrue(result['output_repairs'])

    def test_missing_uncertainty_or_string_boolean_does_not_discard_page(self):
        result = parse_page_output('{"markdown":"SC50-8"}')
        self.assertTrue(result['uncertain'])
        self.assertIn('uncertainty_defaulted', result['output_repairs'])
        result = parse_page_output('{"markdown":"SC50-8","uncertain":"false"}')
        self.assertFalse(result['uncertain'])

    def test_plain_markdown_is_flagged_and_malformed_json_is_never_salvaged(self):
        result = parse_page_output('# 原文\n\n数量：10。')
        self.assertIn('数量：10。', result['markdown'])
        self.assertTrue(result['uncertain'])
        for raw in ['{"markdown":"partial', '{"markdown":7}', '[]', '']:
            with self.subTest(raw=raw), self.assertRaises(ParseError):
                parse_page_output(raw)

    def test_html_tables_and_image_labels_preserved_as_text(self):
        markdown = ('<h2>清单</h2><table><tr><th>名称</th><th>数量</th></tr>'
                    '<tr><td>螺栓 SC50-8</td><td>10</td></tr></table>'
                    '![端子示意](temporary.png)<img src="https://example.test/x" alt="5mm">')
        result = parse_page_output(json.dumps({'markdown': markdown, 'uncertain': False}))
        text = result['markdown']
        self.assertIn('| 螺栓 SC50-8 | 10 |', text)
        self.assertIn('端子示意5mm', text)
        for asset in ('![', '<img', '<table', 'temporary.png', 'https://example.test'):
            self.assertNotIn(asset, text)

    def test_merged_and_unclosed_tables_keep_text_without_guessing_cells(self):
        for markup in ['<table><tr><td colspan="2">Battery</td><td>100Ah</td></tr></table>',
                       '<table><tr><td>Battery</td><td>100Ah',
                       '<table><tr><td>Battery<td>100Ah<tr><td>Voltage<td>51.2V</table>']:
            result = parse_page_output(json.dumps({'markdown': markup, 'uncertain': False}))
            self.assertIn('Battery', result['markdown'])
            self.assertIn('100Ah', result['markdown'])

    def test_literal_tags_are_inert_but_their_text_is_not_dropped(self):
        result = parse_page_output(json.dumps({'markdown': '<script>source code</script> <resource-0> a < b',
                                              'uncertain': False}))
        self.assertIn('source code', result['markdown'])
        self.assertIn('&lt;resource-0>', result['markdown'])
        self.assertNotIn('<script', result['markdown'])
        self.assertIn('a &lt; b', result['markdown'])


class OutputRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_none_is_explicit_on_first_request(self):
        bodies = []
        def handler(request):
            bodies.append(json.loads(request.content))
            self.assertEqual(bodies[-1]["reasoning_effort"], "none")
            return response()
        result, model = await self.call(handler)
        self.assertEqual(model.calls, 1)
        self.assertEqual(len(bodies), 1)

    async def test_explicit_reasoning_error_is_not_retried_unchanged(self):
        attempts = []
        def handler(request):
            attempts.append(json.loads(request.content))
            return httpx.Response(400, json={"error": {"message":
                "reasoning_effort does not support null; supported values: 'none'"}})
        with self.assertRaises(ParseError) as raised:
            await self.call(handler)
        self.assertEqual(raised.exception.code, "model_http_error")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["reasoning_effort"], "none")

    async def call(self, handler, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'page.jpg'
            Image.new('RGB', (10, 10), 'red').save(image)
            settings = Settings(api_key=KEY, litellm_base_url='https://gateway.example',
                                litellm_api_key='test', model_retries=0, **kwargs)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                model = ModelClient(settings, client, tenant_id=12345)
                try:
                    return await model.read_page('task_test', 1, image, 'SC50-8', 'page.pdf'), model
                finally:
                    self.last_model = model

    async def test_structured_request_and_format_retry_account_for_all_tokens(self):
        calls = []
        def handler(request):
            body = json.loads(request.content)
            calls.append(body)
            self.assertEqual(body['reasoning_effort'], 'none')
            self.assertEqual(body['response_format']['type'], 'json_schema')
            self.assertTrue(body['response_format']['json_schema']['strict'])
            return response('{"markdown":"bad \\q"}') if len(calls) == 1 else response()
        result, model = await self.call(handler)
        self.assertIn('51.2V', result['markdown'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(model.calls, 2)
        self.assertEqual(model.usage['total_tokens'], 30)
        self.assertEqual(model.stats['observed_attempts'], 2)
        self.assertEqual(model.stats['retries'], 1)
        self.assertEqual(model.stats['success'], 1)
        self.assertEqual(model.stats['errors']['invalid_model_output'], 1)
        self.assertIn('complete original page', calls[1]['messages'][-1]['content'])

    async def test_truncated_response_regenerated_with_larger_budget(self):
        bodies = []
        def handler(request):
            bodies.append(json.loads(request.content))
            return response(finish='length') if len(bodies) == 1 else response()
        result, model = await self.call(handler)
        self.assertIn('数量：10', result['markdown'])
        self.assertEqual(model.calls, 2)
        self.assertEqual(bodies[1]['max_completion_tokens'], bodies[0]['max_completion_tokens'] * 2)

    async def test_exhausted_format_retries_remain_bounded(self):
        with self.assertRaises(ParseError) as caught:
            await self.call(lambda request: response('{"markdown":'))
        self.assertEqual(caught.exception.code, 'invalid_model_output')
        self.assertEqual(self.last_model.calls, 3)
        self.assertEqual(self.last_model.usage['total_tokens'], 45)

    async def test_empty_page_is_regenerated_but_not_returned_successfully(self):
        calls = []
        def handler(request):
            calls.append(request)
            return response('{"markdown":"","uncertain":false}') if len(calls) == 1 else response()
        result, model = await self.call(handler)
        self.assertTrue(result['markdown'])
        self.assertEqual(model.calls, 2)

    async def test_only_explicit_format_errors_allow_protocol_fallback(self):
        formats = []
        def handler(request):
            formats.append(json.loads(request.content).get('response_format', {}).get('type'))
            if len(formats) < 3:
                return httpx.Response(400, json={'error': 'response_format is not supported'})
            return response()
        _, model = await self.call(handler)
        self.assertEqual(formats, ['json_schema', 'json_object', None])
        self.assertEqual(model.calls, 3)
        self.assertEqual(model.usage['total_tokens'], 15)
        for status, error in [(401, 'model_auth_failed'), (400, 'model_http_error')]:
            calls = []
            def fail(request):
                calls.append(request)
                return httpx.Response(status, text='private response details')
            with self.assertRaises(ParseError) as caught:
                await self.call(fail)
            self.assertEqual(caught.exception.code, error)
            self.assertEqual(len(calls), 1)

    async def test_model_refusal_is_never_treated_as_markdown_or_retried(self):
        for reply in [response('Cannot process', refusal='refused'), response(finish='content_filter')]:
            with self.assertRaises(ParseError) as caught:
                await self.call(lambda request: reply)
            self.assertEqual(caught.exception.code, 'model_refused')
            self.assertEqual(self.last_model.calls, 1)


class PageRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_visual_steps_link_to_crop_of_the_actual_page(self):
        async def handler(request):
            return response(json.dumps({
                "markdown": "Installation drawing.", "uncertain": False,
                "figures": [
                    {"left": 0, "top": 0, "right": 420, "bottom": 1000,
                     "steps": ["Insert the latch from the left."]},
                    {"left": -1, "top": 0, "right": 300, "bottom": 500,
                     "steps": ["Invalid coordinates."]},
                    {"left": 600, "top": 0, "right": 900, "bottom": 1000,
                     "steps": ["Preserve the page when the figure box has no detail."]}],
            }))
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(api_key=KEY, data_dir=Path(tmp), litellm_base_url="https://gateway.example",
                                litellm_api_key="test", model_retries=0)
            jobs = Jobs(settings)
            await jobs.start()
            await jobs.client.aclose()
            jobs.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                task_id, directory = jobs.reserve("drawing.pdf", tenant_id=101, figures=True)
                image = Image.new("RGB", (160, 160), "red")
                image.paste("blue", (80, 0, 160, 160))
                image.paste("white", (10, 10, 48, 38))
                image.save(directory / "source.pdf", "PDF")
                jobs.submit(task_id, (directory / "source.pdf").stat().st_size)
                await asyncio.wait_for(jobs.queue.join(), 10)
                state = jobs.read(task_id)
                self.assertEqual(state["status"], "completed", state.get("error"))
                text = (directory / "knowledge.md").read_text()
                self.assertIn("Insert the latch from the left.", text)
                self.assertIn("<!-- docbreaker-figure:figure-1-1.jpg -->", text)
                self.assertNotIn("Invalid coordinates", text)
                self.assertFalse((directory / "figure-1-2.jpg").exists())
                with Image.open(directory / "figure-1-1.jpg") as crop:
                    r, _, b = crop.getpixel((crop.width // 2, crop.height // 2))
                    self.assertGreater(r, b)
                    self.assertLess(crop.width, crop.height)
                with Image.open(directory / "figure-1-3.jpg") as fallback:
                    self.assertEqual(fallback.width, fallback.height)
            finally:
                await jobs.stop()

    async def test_unreadable_scan_page_is_skipped_without_losing_siblings(self):
        calls = []
        async def handler(request):
            body = json.loads(request.content)
            page = json.loads(body['messages'][1]['content'][0]['text'].split('Source data follows:\n')[1])['page']
            calls.append(page)
            if page == 1:
                return response('{"markdown":')
            return response(json.dumps({'markdown': f'Original page {page}.', 'uncertain': False}))
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(api_key=KEY, data_dir=Path(tmp), litellm_base_url='https://gateway.example',
                                litellm_api_key='test', model_retries=0)
            jobs = Jobs(settings)
            await jobs.start()
            await jobs.client.aclose()
            jobs.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                task_id, directory = jobs.reserve('scan.pdf', tenant_id=101)
                Image.new('RGB', (20, 20), 'red').save(directory/'source.pdf', 'PDF', save_all=True,
                    append_images=[Image.new('RGB', (20, 20), 'blue'), Image.new('RGB', (20, 20), 'green')])
                jobs.submit(task_id, (directory/'source.pdf').stat().st_size)
                await asyncio.wait_for(jobs.queue.join(), 10)
                state = jobs.read(task_id)
                self.assertEqual(state['status'], 'completed', state.get('error'))
                self.assertEqual(state['processed_pages'], 3)
                self.assertEqual(state['missing_content_pages'], [1])
                self.assertEqual(state['degraded_pages'], [{'page': 1, 'code': 'invalid_model_output', 'missing_content': True, 'missing_visual_content': True}])
                self.assertEqual(calls.count(1), 3)
                self.assertEqual(calls.count(2), 1)
                self.assertEqual(calls.count(3), 1)
                text = (directory/'knowledge.md').read_text()
                self.assertIn('Original page 2.', text)
                self.assertIn('Original page 3.', text)
                self.assertNotIn('markdown', text)
                self.assertTrue(state['warnings'])
            finally:
                await jobs.stop()

    async def test_failed_closer_look_retains_first_complete_flagged_extraction(self):
        calls = []
        def handler(request):
            calls.append(request)
            return response(json.dumps({'markdown': 'Original 51.2V [illegible]', 'uncertain': True})) if len(calls) == 1 else response('{"markdown":')
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Jobs(Settings(api_key=KEY, data_dir=Path(tmp), litellm_base_url='https://gateway.example',
                                 litellm_api_key='test', model_retries=0))
            await jobs.start()
            await jobs.client.aclose()
            jobs.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                task_id, directory = jobs.reserve('scan.pdf', tenant_id=12345)
                Image.new('RGB', (20,20), 'red').save(directory/'source.pdf', 'PDF')
                jobs.submit(task_id, (directory/'source.pdf').stat().st_size)
                await asyncio.wait_for(jobs.queue.join(), 10)
                state = jobs.read(task_id)
                self.assertEqual(state['status'], 'completed', state.get('error'))
                self.assertTrue(state['pages'][0]['uncertain'])
                self.assertIn('closer_look_failed', state['pages'][0]['output_repairs'])
                self.assertTrue(state['warnings'])
                self.assertIn('51.2V [illegible]', (directory/'knowledge.md').read_text())
                self.assertEqual(len(calls), 4)
            finally:
                await jobs.stop()

    async def test_image_limit_on_closer_look_retains_first_complete_flagged_extraction(self):
        calls = []
        def handler(request):
            calls.append(request)
            return response(json.dumps({'markdown': 'Original 51.2V [illegible]', 'uncertain': True})) if len(calls) == 1 else httpx.Response(400, json={'error': {'message': 'The image requires 31000 patches, exceeding the limit of 30000'}})
        with tempfile.TemporaryDirectory() as tmp:
            jobs = Jobs(Settings(api_key=KEY, data_dir=Path(tmp), litellm_base_url='https://gateway.example',
                                 litellm_api_key='test', model_retries=0))
            await jobs.start()
            await jobs.client.aclose()
            jobs.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                task_id, directory = jobs.reserve('scan.pdf', tenant_id=12345)
                Image.new('RGB', (20,20), 'red').save(directory/'source.pdf', 'PDF')
                jobs.submit(task_id, (directory/'source.pdf').stat().st_size)
                await asyncio.wait_for(jobs.queue.join(), 10)
                state = jobs.read(task_id)
                self.assertEqual(state['status'], 'completed', state.get('error'))
                self.assertTrue(state['pages'][0]['uncertain'])
                self.assertIn('closer_look_failed', state['pages'][0]['output_repairs'])
                self.assertTrue(state['warnings'])
                self.assertIn('51.2V [illegible]', (directory/'knowledge.md').read_text())
                self.assertEqual(len(calls), 2)
            finally:
                await jobs.stop()


if __name__ == '__main__':
    unittest.main()

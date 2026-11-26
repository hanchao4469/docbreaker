"""Native words must survive visual extraction; failed visual regions remain failures."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from PIL import Image, ImageDraw
from docbreaker.config import Settings
from docbreaker.events import ParseError
from docbreaker.model_client import ModelClient
from docbreaker.model_output import parse_page_output
from docbreaker.pdf_tools import Page, PdfTools
from docbreaker.parser import parse_pdf
from docbreaker.pdf_figures import save_figures
from docbreaker.structure import DocumentStructure
from tests.test_service import KEY

class NativeVisualTests(unittest.IsolatedAsyncioTestCase):
    async def run_split(self, handler):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        source = folder / "source.pdf"
        source.write_bytes(b"test")
        native = "Original safety instructions: tighten bolt A before connecting cable B."
        async def extract(*args, **kwargs):
            return Page(1, native, ["visual_content"], 600, 800, native)
        async def render(*args, **kwargs):
            image = folder / "page-1.jpg"
            Image.new("RGB", (100,100), "gray").save(image)
            return image
        settings = Settings(api_key=KEY, litellm_base_url="https://gateway.example", litellm_api_key="test", model_retries=0, model_output_retries=0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = ModelClient(settings, client, tenant_id=101, figures=True)
            with patch.object(PdfTools, "inspect", return_value=1), patch.object(PdfTools, "document_structure", return_value=DocumentStructure()), patch.object(PdfTools, "extract", extract), patch.object(PdfTools, "render", render):
                result = await parse_pdf(source, "manual.pdf", "test-native-split", settings, model, lambda fields: None)
            return result, (folder / "knowledge.md").read_text(), model

    async def test_native_order_and_visual_relationships_both_survive(self):
        def handler(request):
            body=json.loads(request.content)
            self.assertEqual(request.headers["x-litellm-end-user-id"], "101")
            self.assertEqual(body["reasoning_effort"], "none")
            self.assertIn("ALREADY", body["messages"][0]["content"])
            return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":json.dumps({"markdown":"The arrow points from A to B; insertion depth 5 mm.","uncertain":False,"figures":[]})}}]})
        result, text, model=await self.run_split(handler)
        self.assertIn("tighten bolt A before connecting cable B",text)
        self.assertIn("from A to B; insertion depth 5 mm",text)
        self.assertEqual(result["pages"][0]["parser"],"hybrid")
        self.assertEqual(model.calls,1)

    async def test_no_visual_information_can_be_empty_without_erasing_native(self):
        def handler(request):
            return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":'{"markdown":"","uncertain":false,"figures":[]}'}}]})
        result,text,model=await self.run_split(handler)
        self.assertIn("Original safety instructions",text)
        self.assertFalse(result["pages"][0]["uncertain"])
        with self.assertRaises(ParseError):
            parse_page_output('{"markdown":"","uncertain":false}')

    async def test_failed_visual_call_retains_native_and_marks_degradation(self):
        def handler(request):
            return httpx.Response(200,json={"choices":[{"finish_reason":"stop","message":{"content":'{"markdown":"unfinished'}}]})
        result, text, model = await self.run_split(handler)
        self.assertIn("Original safety instructions", text)
        self.assertNotIn('unfinished', text)
        self.assertEqual(result['pages'][0]['parser'], 'local_fallback')
        self.assertTrue(result['pages'][0]['uncertain'])
        self.assertEqual(result['degraded_pages'][0]['page'], 1)
        self.assertFalse(result['degraded_pages'][0]['missing_content'])


class FigureLabelBoundsTests(unittest.TestCase):
    def test_crop_includes_quoted_native_label_outside_model_box(self):
        with tempfile.TemporaryDirectory() as temp:
            image=Path(temp)/"page.jpg"
            page=Image.new("RGB",(200,200),"white");draw=ImageDraw.Draw(page)
            draw.rectangle((20,40,90,52),fill="red")
            draw.rectangle((30,110,150,170),fill="black")
            page.save(image)
            figures=save_figures(image,[{"left":100,"top":500,"right":900,"bottom":900,"steps":["LOCK PIN ↓",7]}, {"left":100,"top":500,"right":900,"bottom":900,"steps":["Reference"]}],1,24,
                                 [{"text":"LOCK PIN","bounds":(.1,.2,.45,.26)}])
            self.assertEqual(len(figures),2)
            self.assertTrue(figures[-1]["source_page"])
            with Image.open(Path(temp)/figures[-1]["name"]) as original:
                self.assertEqual(original.size,(200,200))
            with Image.open(Path(temp)/figures[0]["name"]) as crop:
                self.assertGreater(crop.height,130)
                self.assertTrue(any(r>150 and g<100 and b<100 for r,g,b in crop.convert("RGB").getdata()))

"""Visual text must not depend on image publishing; partial pages remain retryable."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from PIL import Image
from docbreaker.config import Settings
from docbreaker.model_client import ModelClient, native_text_coverage
from docbreaker.parser import parse_pdf
from docbreaker.pdf_tools import PdfTools, Page
from docbreaker.structure import DocumentStructure
from tests.test_service import KEY


class CompletenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_vector_text_is_inspected_without_image_export(self):
        text='A clear original paragraph with enough native words to look complete.'
        xml=('<doc><page width="600" height="800"><line><word xMin="20" yMin="20" '
             'xMax="550" yMax="32">'+text+'</word></line></page></doc>').encode()
        async def run(tool, args, *unused):
            if tool == 'pdfimages':return b''
            if tool == 'pdftocairo':return b'<svg><path d="M 0 0 L 30 20 L 100 50 Z"/></svg>'
            return xml if '-bbox-layout' in args else text.encode()
        tools=PdfTools(Settings(api_key=KEY),'vector-text')
        with patch.object(tools,'run',run):page=await tools.extract(Path('/tmp/source.pdf'),1)
        self.assertIn('visual_content',page.reasons)
        self.assertEqual(page.native_markdown,text)

    async def test_native_omission_is_flagged_for_closer_read(self):
        native='Safety warning before assembly always retain original packaging until every component is checked and installed correctly.'
        def handler(request):
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'markdown':'Safety warning','uncertain':False})}}]})
        with tempfile.TemporaryDirectory() as temp:
            image=Path(temp)/'page.jpg';Image.new('RGB',(80,80),'gray').save(image)
            settings=Settings(api_key=KEY,litellm_base_url='https://gateway.example',litellm_api_key='test')
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                model=ModelClient(settings,client,tenant_id=1001)
                result=await model.read_page('coverage',1,image,native,'manual.pdf')
        self.assertTrue(result['uncertain'])
        self.assertIn('native_text_omitted',result['uncertainty_reasons'])
        self.assertEqual(native_text_coverage(native,native.upper()),1)
        self.assertEqual(native_text_coverage('8',''),1)

    async def test_page_number_fallback_is_missing_and_retried(self):
        success=False
        def handler(request):
            content=json.dumps({'markdown':'Read the safety instructions before assembly.','uncertain':False}) if success else '{"markdown":'
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':content}}]})
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);source=folder/'source.pdf';source.write_bytes(b'test')
            async def extract(*args,**kwargs):return Page(1,'2',['sparse_native_text'],600,800)
            async def render(*args,**kwargs):
                image=folder/'page.jpg';Image.new('RGB',(80,80),'gray').save(image);return image
            settings=Settings(api_key=KEY,litellm_base_url='https://gateway.example',litellm_api_key='test',model_retries=0,model_output_retries=0)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with patch.object(PdfTools,'inspect',return_value=1),patch.object(PdfTools,'document_structure',return_value=DocumentStructure()),patch.object(PdfTools,'extract',extract),patch.object(PdfTools,'render',render):
                    model=ModelClient(settings,client,tenant_id=1001)
                    result=await parse_pdf(source,'manual.pdf','missing',settings,model,lambda fields:None)
                    self.assertEqual(result['missing_content_pages'],[1])
                    self.assertTrue(result['degraded_pages'][0]['missing_visual_content'])
                    success=True
                    result=await parse_pdf(source,'manual.pdf','missing',settings,model,lambda fields:None)
                    self.assertEqual(result['missing_content_pages'],[])
                    self.assertIn('safety instructions',(folder/'knowledge.md').read_text())
                    self.assertEqual(model.calls,2)

    async def test_diagram_only_page_keeps_steps_and_original_image(self):
        def handler(request):
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                'markdown':'','uncertain':False,'figures':[{'left':0,'top':0,'right':1000,'bottom':1000,
                'steps':['Two people pull the frame outward together.']}]})}}]})
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);source=folder/'source.pdf';source.write_bytes(b'test')
            async def extract(*args,**kwargs):return Page(1,'',['no_native_text'],600,800)
            async def render(*args,**kwargs):
                from PIL import ImageDraw
                path=folder/'page.jpg';image=Image.new('RGB',(100,100),'white')
                ImageDraw.Draw(image).rectangle((20,20,80,80),fill='black');image.save(path);return path
            settings=Settings(api_key=KEY,litellm_base_url='https://gateway.example',litellm_api_key='test')
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                model=ModelClient(settings,client,tenant_id=1001,figures=True)
                with patch.object(PdfTools,'inspect',return_value=1),patch.object(PdfTools,'document_structure',return_value=DocumentStructure()),patch.object(PdfTools,'extract',extract),patch.object(PdfTools,'render',render):
                    result=await parse_pdf(source,'manual.pdf','diagram-only',settings,model,lambda fields:None)
            self.assertFalse(result['degraded_pages'])
            text=(folder/'knowledge.md').read_text()
            self.assertIn('pull the frame outward together',text)
            self.assertIn('docbreaker-figure:figure-1-1.jpg',text)
            self.assertTrue((folder/'figure-1-1.jpg').exists())

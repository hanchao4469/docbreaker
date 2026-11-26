"""Preserve source transcription independently of verified AI figure interpretations."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from PIL import Image, ImageDraw
from docbreaker.config import Settings
from docbreaker.model_client import ModelClient
from docbreaker.parser import parse_pdf
from docbreaker.pdf_tools import PdfTools, Page
from docbreaker.structure import DocumentStructure
from tests.test_service import KEY


class SourceVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, fail_review=False, short_closer=False):
        original='EN - Keep original wording.\nDE - Originalwortlaut beibehalten.\nES - Conservar las palabras originales.'
        requests=[]
        def handler(request):
            body=json.loads(request.content);context=json.loads(body['messages'][1]['content'][0]['text'].split('Source data follows:\n',1)[1]);requests.append(context)
            if 'proposed_figures' in context:
                if fail_review is True:return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"markdown":'}}]})
                result={'markdown':'Invented rewrite must never replace source text.','uncertain':fail_review == 'uncertain','figures':[{'left':0,'top':0,'right':1000,'bottom':1000,'steps':['Slide the adjustable section in the arrow direction.']}]}
            else:
                result={'markdown':('EN - Keep original wording.' if short_closer and context['closer_look'] else original),'uncertain':short_closer,'figures':[{'left':0,'top':0,'right':1000,'bottom':1000,'steps':['Fold the frame inward.']}]}
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result)}}]})
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source.pdf';source.write_bytes(b'fixture')
            async def extract(*args,**kwargs):return Page(1,'EN\nDE\nES',['visual_content'],600,800,'EN\nDE\nES')
            async def render(*args,**kwargs):
                path=root/'page.jpg';image=Image.new('RGB',(100,100),'white');ImageDraw.Draw(image).rectangle((20,20,80,80),fill='black');image.save(path);return path
            settings=Settings(api_key=KEY,litellm_base_url='https://gateway.example',litellm_api_key='test',model_retries=0,model_output_retries=0)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                model=ModelClient(settings,client,tenant_id=1001,figures=True)
                with patch.object(PdfTools,'inspect',return_value=1),patch.object(PdfTools,'document_structure',return_value=DocumentStructure()),patch.object(PdfTools,'extract',extract),patch.object(PdfTools,'render',render):
                    result=await parse_pdf(source,'instructions.pdf','verify-source',settings,model,lambda fields:None)
            return original,(root/'knowledge.md').read_text(),result,requests

    async def test_labels_trigger_full_transcription_and_review_cannot_rewrite_original(self):
        original,text,result,requests=await self.run_case()
        self.assertFalse(requests[0]['native_text_already_extracted'])
        self.assertIn(original,text)
        self.assertIn('AI diagram interpretation',text)
        self.assertIn('Slide the adjustable section',text)
        self.assertNotIn('Fold the frame',text)
        self.assertNotIn('Invented rewrite',text)
        self.assertEqual(len(requests),2)

    async def test_failed_review_withholds_claim_but_keeps_original_text_and_image(self):
        original,text,result,requests=await self.run_case(True)
        self.assertIn(original,text)
        self.assertNotIn('Fold the frame',text)
        self.assertIn('refer to the original image',text)
        self.assertIn('docbreaker-figure:',text)
        self.assertIn('figure_verification_failed',result['pages'][0]['uncertainty_reasons'])

    async def test_uncertain_review_withholds_even_grammatical_actions(self):
        original,text,result,requests=await self.run_case('uncertain')
        self.assertIn(original,text)
        self.assertNotIn('Slide the adjustable section',text)
        self.assertNotIn('Fold the frame',text)
        self.assertIn('refer to the original image',text)
        self.assertTrue(result['pages'][0]['uncertain'])

    async def test_shorter_closer_read_cannot_erase_multilingual_transcription(self):
        original,text,result,requests=await self.run_case(short_closer=True)
        self.assertIn(original,text)
        self.assertIn('fuller_initial_transcription_retained',result['pages'][0]['output_repairs'])

    def test_quantities_cannot_disappear_from_native_coverage_check(self):
        from docbreaker.model_client import native_text_coverage
        source='⑥ Rope ×6   ⑦ Stake ×6\n12'
        self.assertLess(native_text_coverage(source,'Rope Stake'),.9)
        self.assertEqual(native_text_coverage(source,'⑥ Rope ×6\n⑦ Stake ×6'),1)

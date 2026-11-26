import unittest
from docbreaker.structure import DocumentStructure
from docbreaker.model_output import normalize_markdown


def xml(pages):
    return ('<pdf2xml>'+''.join('<page number="%s" height="1000"><fontspec id="0" size="16" family="Sans"/><fontspec id="1" size="22" family="Sans"/><fontspec id="2" size="18" family="Sans"/>'%n+content+'</page>' for n,content in enumerate(pages,1))+'</pdf2xml>').encode()


def line(text, font=0, top=100):
    return f'<text top="{top}" left="40" width="500" height="20" font="{font}">{text}</text>'


class StructureTests(unittest.TestCase):
    def test_global_levels_and_same_size_bold_heading(self):
        body=line('This is a long body paragraph with enough characters to establish the body font.',top=200)
        d=DocumentStructure.from_xml(xml([line('<b>Title</b>',1)+body,line('<b>Installing</b>',2)+body]))
        self.assertEqual(d.format_page(1,'Title'), '# Title')
        self.assertEqual(d.format_page(2,'Installing'), '## Installing')
        self.assertEqual(d.format_page(2,'Different title'), 'Different title')

    def test_repeated_margin_requires_three_pages(self):
        body=line('This is a long body paragraph with enough characters to establish the body font.',top=200)
        page=line('Repeated footer',top=970)+body
        self.assertEqual(DocumentStructure.from_xml(xml([page]*3)).format_page(1,'Repeated footer\nBody'),'Body')
        self.assertEqual(DocumentStructure.from_xml(xml([page]*2)).format_page(1,'Repeated footer'),'Repeated footer')

    def test_existing_fences_and_tables_are_untouched(self):
        body=line('This is a long body paragraph with enough characters to establish the body font.',top=200)
        d=DocumentStructure.from_xml(xml([line('<b>Title</b>',1)+body]))
        text='```\nTitle\n```\n| Title | value |'
        self.assertEqual(d.format_page(1,text),text)

    def test_markdown_table_cell_break_stays_inline_and_safe(self):
        text='| Name | Value |\n| --- | --- |\n| A<br/>B | <img src=x onerror=alert(1)> |'
        out,_=normalize_markdown(text)
        self.assertIn('| A<br>B |',out)
        self.assertEqual(len(out.splitlines()),3)
        self.assertNotIn('onerror',out)

    def test_entities_are_rejected(self):
        with self.assertRaises(ValueError):
            DocumentStructure.from_xml(b'<!DOCTYPE x [<!ENTITY x "payload">]><pdf2xml/>')


class PopplerStructureTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pdf_typography_pass(self):
        import shutil
        import tempfile
        from pathlib import Path
        from docbreaker.config import Settings
        from docbreaker.pdf_tools import PdfTools
        if not shutil.which('pdftohtml'):
            self.skipTest('Poppler unavailable')
        commands = b'BT /F1 22 Tf 50 760 Td (Document title) Tj ET\nBT /F1 12 Tf 50 700 Td (This is a long source paragraph with enough characters to establish the body font size.) Tj ET'
        objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [4 0 R] /Count 1 >>',b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>',b'<< /Length '+str(len(commands)).encode()+b' >>\nstream\n'+commands+b'\nendstream']
        data=b'%PDF-1.4\n';offsets=[0]
        for n,obj in enumerate(objects,1):
            offsets.append(len(data));data+=f'{n} 0 obj\n'.encode()+obj+b'\nendobj\n'
        xref=len(data);data+=b'xref\n0 6\n0000000000 65535 f \n'+b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets[1:])+f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'source.pdf';path.write_bytes(data)
            d=await PdfTools(Settings(api_key='x'*32),'structure-test').document_structure(path)
            self.assertEqual(d.format_page(1,'Document title'),'# Document title')
            self.assertEqual(d.format_page(1,'Unseen content'),'Unseen content')

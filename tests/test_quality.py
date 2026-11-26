import unittest
from docbreaker.model_output import normalize_markdown
from docbreaker.layout import restore_native_headings


class QualityTests(unittest.TestCase):
    def test_span_table_preserves_relations_and_strips_active_attributes(self):
        source = '<table onclick="alert(1)"><tr><th colspan="2">Group</th></tr><tr><td rowspan="2">A</td><td>12 &amp; 3</td></tr><tr><td>15</td></tr></table>'
        result, _ = normalize_markdown(source)
        self.assertIn('<th colspan="2">Group</th>', result)
        self.assertIn('<td rowspan="2">A</td>', result)
        self.assertIn('<td>12 &amp; 3</td>', result)
        self.assertNotIn('onclick', result)
        self.assertNotIn('alert', result)

    def test_math_and_code_do_not_become_html_entities(self):
        source = '$x < y$ and $$a & b < c$$\n\n```html\n<table><tr><td>literal</td></tr></table>\n```\n\n`<script>`'
        self.assertEqual(normalize_markdown(source)[0], source)

    def test_table_cannot_reintroduce_active_html(self):
        source = '<table><tr><td colspan="2" style="color:red"><img src="https://bad"><script>alert(1)</script>A</td></tr></table>'
        result, _ = normalize_markdown(source)
        self.assertNotIn('<script', result)
        self.assertNotIn('<img', result)
        self.assertNotIn('style=', result)
        self.assertIn('A</td>', result)

    def test_protected_tokens_cannot_collide_with_source(self):
        source = 'DOCBREAKERPROTECTED0END $a < b$'
        self.assertEqual(normalize_markdown(source)[0], source)

    def test_native_heading_uses_geometry_without_deleting_body(self):
        lines = [('Introduction', 10, 20)] + [('A sufficiently long ordinary sentence with useful original source text.', 50+i*20, 10) for i in range(5)]
        xml = '<doc><page width="600" height="800">' + ''.join(f'<line><word xMin="20" yMin="{y}" xMax="500" yMax="{y+h}">{t}</word></line>' for t,y,h in lines) + '</page></doc>'
        text = '\n'.join(t for t,_,_ in lines)
        result = restore_native_headings(xml.encode(), text)
        self.assertTrue(result.startswith('# Introduction\n'))
        self.assertEqual(result.removeprefix('# '), text)
        # The exact same label at body size must not become a heading.
        self.assertEqual(restore_native_headings(xml.replace('yMax="30"','yMax="20"').encode(),text),text)


    def test_large_same_baseline_table_cells_are_not_headings(self):
        body = [('A long body paragraph with enough source text to establish body typography.', 60+i*20, 10, 20, 500) for i in range(5)]
        lines = [('Product', 10, 20, 20, 200), ('Quantity', 10, 20, 250, 500)] + body
        xml = '<doc><page width="600" height="800">' + ''.join(f'<line><word xMin="{x}" yMin="{y}" xMax="{right}" yMax="{y+h}">{t}</word></line>' for t,y,h,x,right in lines) + '</page></doc>'
        text = '\n'.join(t for t,*_ in lines)
        self.assertEqual(restore_native_headings(xml.encode(), text), text)


    def test_math_delimiters_do_not_allow_active_resource_tags(self):
        result, _ = normalize_markdown('$x < y$ $$<img src="x" onerror="alert(1)">$$')
        self.assertIn('$x < y$', result)
        self.assertNotIn('<img', result)


class SparseNativeTests(unittest.TestCase):
    def test_year_and_bullets_cannot_bypass_visual_extraction(self):
        from docbreaker.pdf_tools import route_reasons
        xml = b'<doc><page width="600" height="800"><line><word xMin="10" yMin="10" xMax="40" yMax="20">2006</word></line></page></doc>'
        self.assertIn('sparse_native_text', route_reasons('2006\n•\n•\n•\n– 139 –', xml, 0))
        self.assertNotIn('sparse_native_text', route_reasons('A normal readable document contains complete source sentences.', xml, 0))

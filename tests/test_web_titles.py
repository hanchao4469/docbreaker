import unittest
from docbreaker.web_extract import extract

URL = 'https://site.example/team/'
BODY = '<p>Details about the team, its players and the latest activities, including detailed schedules and useful visitor information.</p>'


class WebTitleTests(unittest.TestCase):
    def test_browser_title_precedes_logo_and_section_heading(self):
        html = ('<html><head><title>チーム・選手 | 北海道日本ハムファイターズ</title>'
                '<meta property="og:title" content="Other share title"></head><body>'
                '<header><h1>FIGHTERS</h1></header><main><h1>チーム・選手</h1>' + BODY + '</main></body></html>')
        page = extract(html, URL)
        self.assertEqual(page['title'], 'チーム・選手 | 北海道日本ハムファイターズ')
        self.assertTrue(page['markdown'].startswith('# チーム・選手 &#124; 北海道日本ハムファイターズ'))

    def test_empty_browser_title_uses_share_title(self):
        page = extract('<head><title> </title><meta property="og:title" content="Page title"></head>'
                       '<main><h1>Section heading</h1>' + BODY + '</main>', URL)
        self.assertEqual(page['title'], 'Page title')

    def test_no_metadata_uses_content_heading_not_header_logo(self):
        page = extract('<body><header><h1>Company logo</h1></header><h1>Actual page</h1>' + BODY + '</body>', URL)
        self.assertEqual(page['title'], 'Actual page')

    def test_empty_heading_does_not_hide_browser_title(self):
        page = extract('<head><title>Correct page</title></head><main><h1><img src="/logo.png"></h1>'
                       + BODY + '</main>', URL)
        self.assertEqual(page['title'], 'Correct page')

import io
import json
import unittest
from unittest.mock import AsyncMock

import httpx
from PIL import Image

from docbreaker.config import Settings
from docbreaker.web_enrich import WebEnricher
from docbreaker.web_extract import extract

PAGE = "https://example.com/trips"
MAP = "https://example.com/hotel-map.jpg"
HTML = b"""<main><h1>Travel information</h1><p>Schedule, prices and tour information for passengers at the airport.</p>
<h2>Itinerary</h2><table><tr><td>Day</td><td>Location</td><td>Price</td></tr>
<tr><td rowspan="2">Monday</td><td>Nago</td><td>22000 yen</td></tr><tr><td>Kunigami</td><td>9800 yen</td></tr></table>
<img src="/hotel-map.jpg" alt="Map"><img src="https://third.example/map.png" alt="Map"><p>After the map: reservation instructions.</p></main>"""


class TestWebEnrichment(unittest.IsolatedAsyncioTestCase):
    def test_keeps_text_after_malformed_break_and_descriptive_image_alt(self):
        alt = "STEP1 Choose the plan; STEP2 Reserve the trip; STEP3 Submit the form."
        source = f"""<main><p>Read these important instructions before booking your trip.
        </br>Booking is complete only after submitting the reference.</p>
        <img src="/steps.jpg" alt="{alt}"><img src="/steps-mobile.jpg" alt="{alt}">
        <a href="/form">Booking form</a></main>"""
        md = extract(source, PAGE)["markdown"]
        self.assertIn("Booking is complete only after submitting the reference.", md)
        self.assertEqual(md.count(alt), 1)
        self.assertIn("[Booking form](https://example.com/form)", md)

    async def test_stale_image_alt_never_becomes_a_searchable_fact(self):
        url = "https://example.com/promo-banner.jpg"
        stale = "限定のオリジナル帽子をプレゼント：先着250名 / 2025デザイン"
        html = f"""<main><p>Travel information, schedules and current booking conditions for all event participants.</p>
        <img src="/promo-banner.jpg" alt="{stale}">
        <table><tr><th>定員</th></tr><tr><td>先着150名</td></tr></table></main>"""
        self.assertNotIn("先着250名", extract(html, PAGE)["markdown"])
        parsed = extract(html, PAGE, locate_images=True)
        self.assertEqual(parsed["resources"][0]["label"], stale)
        image_only = extract(f'<main><img src="/promo-banner.jpg" alt="{stale}"></main>',
                             PAGE, locate_images=True)
        self.assertTrue(image_only["_requires_image_text"])
        self.assertNotIn("250", image_only["markdown"])
        enricher = WebEnricher(Settings(api_key="a" * 32, litellm_base_url="https://gateway.example",
                                        litellm_api_key="pdf-key", litellm_web_api_key="web-key"), AsyncMock(), "web-parse")
        try:
            for recognition in ({}, {url: (url, ["先着150名 / 2026デザイン"])}):
                enricher.analyze = AsyncMock(return_value=recognition)
                result = await enricher.for_task("web_test", 101, False)(
                    PAGE, parsed, parsed["_image_positions"], AsyncMock())
                self.assertNotIn("先着250名", result)
                self.assertNotIn("2025", result)
                self.assertIn("先着150名", result)
                if recognition:
                    self.assertIn("2026デザイン", result)
                    self.assertIn(url, result)
        finally:
            await enricher.close()

    async def test_web_gateway_routing_and_source_backed_output(self):
        for gateway_ok in (True, False):
            source = extract(HTML, PAGE, locate_images=True)
            self.assertIn('rowspan="2"', source["markdown"])
            io_image = io.BytesIO()
            Image.new("RGB", (150, 150)).save(io_image, "JPEG")
            fetcher = AsyncMock()
            fetcher.get.return_value = MAP, io_image.getvalue(), "image/jpeg"
            enricher = WebEnricher(Settings(api_key="a" * 32, litellm_base_url="https://gateway.example",
                                            litellm_api_key="pdf-key", litellm_web_api_key="web-key"), fetcher, "web-parse")
            await enricher.client.aclose()

            def respond(request):
                self.assertEqual(request.headers["authorization"], "Bearer web-key")
                self.assertEqual(request.headers["x-litellm-tags"], "web-parse")
                self.assertEqual(request.headers["x-litellm-end-user-id"], "1003")
                self.assertEqual(request.headers["x-litellm-session-id"], "web_test")
                body = json.loads(request.content)
                self.assertEqual(body["reasoning_effort"], "none")
                self.assertEqual(sum(p["type"] == "image_url" for p in body["messages"][1]["content"]), 1)
                if not gateway_ok:
                    return httpx.Response(503)
                return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
                    "headers": {"1": [0]}, "images": {"1": ["Camp: Nago"], "2": ["Unsent image"]}})}}]})

            enricher.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            try:
                permitted = AsyncMock(return_value=True)
                markdown = await enricher.for_task("web_test", 1003, False)(
                    PAGE, source, source["_image_positions"], permitted)
                fetcher.get.assert_awaited_once_with(MAP, PAGE, limit=2_500_000, permitted=permitted, image=True)
                self.assertEqual(markdown.count("9800 yen"), 1)
                self.assertNotIn("Table row and column index", markdown)
                self.assertNotIn("Unsent image", markdown)
                if gateway_ok:
                    self.assertIn(MAP, markdown)
                    self.assertLess(markdown.index("</table>"), markdown.index("Camp: Nago"))
                    self.assertLess(markdown.index("Camp: Nago"), markdown.index("After the map"))
                else:
                    self.assertEqual(markdown, source["markdown"])
                self.assertNotIn("DOCBREAKERIMAGE", markdown)
            finally:
                await enricher.close()

import asyncio
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx

from docbreaker.api import create_app
from docbreaker.config import Settings
from docbreaker.web_crawl import Robots, crawl, sitemap_links
from docbreaker.web_extract import extract
from docbreaker.web_fetch import Fetcher, WebError, normalize, public_address

BASE = "https://store.example/"
KEY = "development-test-key-only-123456789"
PRODUCT = b"""<html><head><title>Trail Bike</title>
<script type="application/ld+json">
{"@type":"ProductGroup","name":"Trail Bike","hasVariant":[{"@type":"Product","name":"Blue","sku":"BL",
"offers":{"price":"899.00","priceCurrency":"USD","availability":"https://schema.org/OutOfStock"}}]}
</script></head><body><nav>LOGIN MENU</nav><main><h1>Trail Bike</h1><h1>Trail Bike</h1>
<div role="dialog">BUY THIS SPAM</div><button>Add to cart</button>
<h2>Specifications</h2><table><tr><th colspan="2" onclick="run()">Electrical</th></tr>
<tr><td>Battery</td><td>48V 20Ah</td></tr></table>
<details><summary>Does it include a warranty?</summary><p>Two years on the motor.</p></details>
<a href="/manual.pdf">User manual</a><img src="/geometry.png" alt="Geometry chart">
<a href="javascript:alert(1)">Accessories</a>
<variant-picker><script type="application/json" data-variants>
[{"id":123,"title":"Blue","sku":"BL","price":89900,"available":false}]</script></variant-picker>
<p>&lt;script&gt; literal text</p><pre><code>x &lt; y</code></pre>
</main><footer>FOOTER NEWSLETTER</footer></body></html>"""


class ExtractionTests(unittest.TestCase):
    def test_preserve_facts_and_remove_chrome(self):
        result = extract(PRODUCT, BASE + "products/trail-bike")
        md = result["markdown"]
        for fact in ("48V 20Ah", "Two years on the motor", "Does it include a warranty?", "899.00 USD",
                     "OutOfStock", "BL", BASE + "manual.pdf", 'colspan="2"', "x < y"):
            self.assertIn(fact, md)
        for junk in ("LOGIN MENU", "BUY THIS SPAM", "FOOTER NEWSLETTER", "onclick", "javascript:", "<script>"):
            self.assertNotIn(junk, md)
        self.assertEqual(md.count("# Trail Bike"), 1)
        self.assertNotIn("geometry.png", md)
        self.assertIn({"url": BASE + "geometry.png", "label": "Geometry chart", "type": "image"},
                      result["resources"])

    def test_robots_and_sitemap(self):
        rules = Robots(b"User-agent: *\nDisallow: /*?*preview=\nDisallow: /support/\nAllow: /support/faq$\n")
        self.assertTrue(rules.allows(BASE + "support/faq"))
        self.assertFalse(rules.allows(BASE + "support/faq/old"))
        self.assertFalse(rules.allows(BASE + "p?page=2&preview=1"))
        self.assertEqual(sitemap_links(b'<urlset xmlns="urn:sitemap"><url><loc>https://store.example/a</loc></url></urlset>'),
                         (False, [BASE + "a"]))
        with self.assertRaises(WebError):
            sitemap_links(b'<!DOCTYPE x [<!ENTITY e "test">]><urlset>&e;</urlset>')
        self.assertEqual(normalize(BASE + "c?page=2&utm_source=x#part"), BASE + "c?page=2")
        for bad in ("127.0.0.1", "10.0.0.1", "100.64.0.1", "169.254.169.254", "::1", "224.1.2.3", "::ffff:127.0.0.1"):
            self.assertFalse(public_address(bad))


class CrawlTests(unittest.IsolatedAsyncioTestCase):
    async def test_breadth_limit_does_not_claim_full_coverage(self):
        def markup(title):
            return (f"<main><h1>{title}</h1><p>A complete and useful page explaining "
                    f"the important specifications, price and maintenance of {title}.</p></main>").encode()

        class Fixture:
            async def get(self, url, seed, limit=0, permitted=None):
                if permitted and not await permitted(url):
                    raise WebError("robots_denied")
                if url.endswith("/robots.txt"):
                    return url, b"User-agent: *\nDisallow: /admin", "text/plain"
                if url.endswith("/sitemap.xml"):
                    content = b"<urlset><url><loc>https://store.example/products/trail</loc></url><url><loc>https://store.example/pages/warranty</loc></url></urlset>"
                    return url, content, "application/xml"
                if url == BASE:
                    return url, markup("Home") + b'<a href="/cart">Cart</a>', "text/html"
                return url, PRODUCT if "/products/" in url else markup("Warranty"), "text/html"

        result = await crawl(Fixture(), BASE, 2, full_site=True)
        self.assertEqual(result["counts"], {"extracted": 2})
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["pages"][1]["type"], "product")

    async def test_single_page_keeps_links_without_crawling_them(self):
        calls = []
        target = BASE + "products/trail-bike"

        class Fixture:
            async def get(self, url, seed, limit=0, permitted=None):
                if permitted and not await permitted(url):
                    raise WebError("robots_denied")
                calls.append(url)
                if url == BASE + "robots.txt":
                    return url, b"Sitemap: https://store.example/sitemap.xml", "text/plain"
                self_body = PRODUCT.replace(b"Accessories</a>", b"Accessories</a><a href='/pages/warranty'>Warranty</a>")
                return url, self_body, "text/html"

        result = await crawl(Fixture(), target, 1, full_site=False)
        self.assertEqual(calls, [BASE + "robots.txt", target])
        self.assertEqual(result["discovery"], [])
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["counts"], {"extracted": 1})
        self.assertIn(BASE + "pages/warranty", result["pages"][0]["markdown"])

    async def test_invalid_robots_only_allows_selected_page_not_recursive_crawl(self):
        for reason in ("redirect_out_of_scope", "robots_not_rules", "network_error", "deny"):
            class Fixture:
                async def get(self, url, seed, limit=0, permitted=None, **kwargs):
                    if permitted and not await permitted(url):
                        raise WebError("robots_denied")
                    if url.endswith("/robots.txt"):
                        if reason == "deny":
                            return url, b"User-agent: *\nDisallow: /", "text/plain"
                        raise WebError(reason)
                    return url, PRODUCT, "text/html"
            result = await crawl(Fixture(), BASE, 1, full_site=False)
            expected = "extracted" if reason in ("redirect_out_of_scope", "robots_not_rules") else (
                "robots_denied" if reason == "deny" else "robots_unavailable")
            self.assertIn(expected, result["counts"])
            result = await crawl(Fixture(), BASE, 1, full_site=True)
            self.assertNotIn("extracted", result["counts"])

    async def test_dns_pinning_and_redirect_recheck(self):
        seen = []

        def response(request):
            seen.append((request.url.host, request.headers["Host"], request.extensions["sni_hostname"]))
            return httpx.Response(302, headers={"Location": "https://www.store.example/next"})

        f = Fetcher()
        await f.client.aclose()
        f.client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        records = [[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))],
                   [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 443))]]
        try:
            with patch.object(asyncio.get_running_loop(), "getaddrinfo", new=AsyncMock(side_effect=records)):
                with self.assertRaisesRegex(WebError, "non_public_address"):
                    await f.get(BASE, BASE)
            self.assertEqual(seen, [("93.184.216.34", "store.example", "store.example")])
        finally:
            await f.close()


class WebApiTests(unittest.TestCase):
    def test_tenant_admission_and_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Settings(api_key=KEY, data_dir=Path(tmp)))
            body = {"tenant_id": 1002, "url": BASE, "max_pages": 1}
            async def fake_crawl(*args, **kwargs):
                return {"pages": [], "attempts": [], "counts": {"http_403": 1}, "discovered": 1, "pending": 0, "discovery": []}
            with patch("docbreaker.web_jobs.crawl", fake_crawl), TestClient(app) as client:
                self.assertEqual(client.post("/v1/web/tasks", json=body).status_code, 401)
                headers = {"Authorization": f"Bearer {KEY}"}
                accepted = client.post("/v1/web/tasks", headers={**headers, "service-id": "legacy-client"}, json=body)
                self.assertEqual(accepted.status_code, 202)
                self.assertIs(accepted.json()["isFullSiteCrawl"], False)
                self.assertEqual(client.post("/v1/web/tasks", headers=headers,
                                             json={**body, "max_pages": 2}).status_code, 400)
                self.assertEqual(client.post("/v1/web/tasks", headers=headers,
                                             json={**body, "isFullSiteCrawl": "false"}).status_code, 422)
                site = client.post("/v1/web/tasks", headers=headers,
                                   json={**body, "tenant_id": 43, "isFullSiteCrawl": True})
                self.assertEqual(site.status_code, 202)
                self.assertIs(site.json()["isFullSiteCrawl"], True)
                result_url = accepted.json()["result_url"]
                self.assertEqual(client.get(result_url, headers=headers, params={"tenant_id": 42}).status_code, 404)
                result = client.get(result_url, headers=headers, params={"tenant_id": 1002})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["service_id"], "web-parse")
                client.post("/v1/admin/drain", headers=headers).raise_for_status()
                self.assertEqual(client.post("/v1/web/tasks", headers=headers, json=body).status_code, 503)
